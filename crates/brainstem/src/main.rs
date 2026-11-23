//! The brainstem: the hub every organ connects to, and the keeper of the
//! being's state. Small, and it never blocks on any one organ.

mod battery;
mod body;
mod brightness;
mod camera;
mod life;
#[cfg(target_os = "linux")]
mod linux_body;
mod machine;
mod peer;
mod stem_state;

use bus::{read_line_capped, topic_matches, Event, LineRead, MAX_LINE};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::{BufReader, Write};
use std::net::Shutdown;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::time::{Duration, Instant};

const HELP: &str = "\
brainstem: the being's event hub and state machine

  brainstem                      listen on the default bus socket
  brainstem --bus PATH           listen somewhere else
  brainstem --require a,b,c      organs that must be present (default face,mind,voice)
  brainstem --think-timeout S    give up on a reply still thinking after S seconds in all
                                 (or set BEING_THINK_TIMEOUT; default 600, or 300 for a
                                 mind that never reports progress)
  brainstem --think-stall S      give up when a mind that reports progress has been
                                 silent for S seconds (or BEING_THINK_STALL; default 45)
  brainstem --speak-timeout S    give up on speech that has stalled for S seconds
  brainstem --state-file PATH    where a restart finds its mute and state (default
                                 stem-state next to the bus socket)
  brainstem --poweroff-cmd CMD   run CMD (instead of systemctl poweroff) when the
                                 battery or a lid closed on battery calls for it;
                                 also enables that policy off the hardware body
  brainstem --mind-dir PATH      where its life record is kept (or set BEING_MIND)
  brainstem --mind-is-a-mount    refuse a mind directory that is not its own
                                 partition, so a failed mount is never mistaken
                                 for a blank mind
  brainstem --appliance-body    use the Linux body unless this is the QEMU lab
  brainstem --log                print every event to stderr
  brainstem --probe-hardware     print what the body finds (lid, battery, AC,
                                 light, temperature, backlight) and exit
  brainstem --sysfs-root PATH    probe a copy of /sys under PATH instead";

/// Set when the system asks the brainstem to stop: time to go to sleep.
static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_stop(_signal: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

/// True if `path` is where a filesystem is mounted.
fn is_mount_point(path: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    let (Ok(here), Some(parent)) = (std::fs::metadata(path), path.parent()) else { return false };
    std::fs::metadata(parent).is_ok_and(|up| up.dev() != here.dev())
}

/// Topics only the brainstem may send. Orders to the voice and the mind are
/// the brainstem's alone, so no organ can mute, stop or cancel another.
const RESERVED: &[&str] = &[
    "organ.*", "stem.*", "state.*", "body.*", "face.*", "camera.policy",
    "voice.mute", "say.stop", "mind.cancel", "hello.ok",
];
/// Reserved topics one organ may send all the same: the topic and the organ.
const RESERVED_EXCEPT: &[(&str, &str)] = &[("body.care", "bodycare")];
/// Lines queued for one organ before it is judged stuck and dropped.
const QUEUE: usize = 512;
/// Inputs queued for the hub before readers wait (each organ's reader blocks
/// rather than growing memory without bound).
const INPUTS: usize = 4096;
/// A write to an organ that takes longer than this ends its connection.
const WRITE_TIMEOUT: Duration = Duration::from_secs(1);
/// After a failed accept (out of descriptors, say), wait before the next.
const ACCEPT_BACKOFF: Duration = Duration::from_millis(200);
const TICK_INTERVAL: Duration = Duration::from_millis(100);

enum Input {
    Joined(u64, SyncSender<String>, UnixStream, Option<String>),
    Line(u64, Event),
    Gone(u64),
    Tick,
}

fn reserved(topic: &str, from: &str) -> bool {
    RESERVED.iter().any(|r| topic_matches(r, topic))
        && !RESERVED_EXCEPT.iter().any(|(t, organ)| *t == topic && *organ == from)
}

/// May this organ send this topic at all? Key presses come from the face
/// (the panel or the console screen) on a hardware body; in the lab and in
/// simulation the typed console stands in for the keyboard. That includes
/// `key.attend` (Space, Return or a click while the eyes rest).
fn allowed_from(topic: &str, from: &str, hardware_body: bool) -> bool {
    if reserved(topic, from) {
        return false;
    }
    // "Stop listening" is the owner's words, so the mind may press mute; it
    // can only turn the microphone off (see `mind_may_press`), never back on.
    if topic == "key.mute" && from == "mind" {
        return true;
    }
    // Typing over Pip from the local menu's typing chat stops it speaking, as
    // Esc does; that is the only key the typing chat may press.
    if topic == "key.interrupt" && from == "keyboard" {
        return true;
    }
    if topic_matches("key.*", topic) && hardware_body && from != "face" {
        return false;
    }
    // The owner's words: on the laptop only the voice hears them, and the
    // local menu's typing chat types them (as a wake, the start of speech
    // and the finished utterance). No other organ may put words in the
    // owner's mouth. In the lab and the simulator any organ stands in, as
    // for the keys.
    if topic_matches("hear.*", topic) && hardware_body {
        return match from {
            "voice" => true,
            "keyboard" => matches!(topic, "hear.wake" | "hear.speech_start" | "hear.partial" | "hear.final" | "hear.cancel"),
            _ => false,
        };
    }
    // What the camera sees is the eyes' alone, everywhere.
    if topic_matches("see.*", topic) && from != "eyes" {
        return false;
    }
    match topic {
        "mind.progress" | "mind.shutdown_ready" | "mind.announce" | "mind.clock" | "mind.dream_committed"
        | "mind.dreaming" => from == "mind",
        // "Brighter" and "dimmer" are the owner's, through the mind.
        "being.brightness" => from == "mind",
        // The owner's "louder" or "no chirps" reaches the voice through the
        // mind; the voice answers with what it really set. "Which voice"
        // and "how well can you hear me" ask the voice the same way.
        "voice.volume" | "voice.chirps" | "voice.level_request" | "voice.engine" => from == "mind",
        // Measured levels, numbers only, from the voice alone.
        "voice.volume_state" | "voice.level" => from == "voice",
        topic if topic_matches("voice.*", topic) => false,
        _ => true,
    }
}

/// On the laptop the mind's `key.mute` must say `on: true`. Letting the
/// microphone listen again stays a key press (M) or the menu, so no reply
/// can reopen the ears. In the lab and the simulator any organ stands in for
/// the keyboard, as `allowed_from` says.
fn mind_may_press(ev: &Event, from: &str, hardware_body: bool) -> bool {
    !hardware_body || ev.topic != "key.mute" || from != "mind" || ev.bool("on") == Some(true)
}

fn next_input(rx: &mpsc::Receiver<Input>, next_tick: Instant) -> Option<Input> {
    let wait = next_tick.saturating_duration_since(Instant::now());
    // An overdue tick (including the lid poll) takes priority over queued bus traffic.
    if wait.is_zero() {
        return Some(Input::Tick);
    }
    match rx.recv_timeout(wait) {
        Ok(input) => Some(input),
        Err(mpsc::RecvTimeoutError::Disconnected) => None,
        Err(mpsc::RecvTimeoutError::Timeout) => Some(Input::Tick),
    }
}

fn next_tick_after(deadline: Instant, finished: Instant) -> Instant {
    let next = deadline + TICK_INTERVAL;
    // A normal tick keeps its phase. After an overrun, leave room for queued
    // organ input instead of repeatedly polling hardware back to back.
    if next > finished { next } else { finished + TICK_INTERVAL }
}

struct Peer {
    name: Option<String>,
    subs: Vec<String>,
    tx: SyncSender<String>,
    /// Kept to end the connection at once when the hub drops this organ.
    stream: UnixStream,
    /// The systemd unit it runs in, when known.
    unit: Option<String>,
}

impl Drop for Peer {
    fn drop(&mut self) {
        let _ = self.stream.shutdown(Shutdown::Both);
    }
}

struct Hub {
    peers: HashMap<u64, Peer>,
    log: bool,
    started: Instant,
}

impl Hub {
    fn name_of(&self, id: u64) -> &str {
        self.peers.get(&id).and_then(|p| p.name.as_deref()).unwrap_or("?")
    }

    /// Deliver to every subscriber except the sender. Returns organs that could
    /// not take it and must be dropped.
    fn route(&self, ev: &Event, from: Option<u64>) -> Vec<u64> {
        if self.log && !matches!(ev.topic.as_str(), "say.level" | "stem.beat" | "face.gaze" | "body.presence") {
            let who = from.map(|id| self.name_of(id)).unwrap_or("brainstem");
            eprint!("[{:9.3}] {who:>9}  {}", self.started.elapsed().as_secs_f64(), ev.to_line());
        }
        let line = ev.to_line();
        let mut stuck = Vec::new();
        for (&id, peer) in &self.peers {
            if Some(id) == from || !peer.subs.iter().any(|s| topic_matches(s, &ev.topic)) {
                continue;
            }
            if peer.tx.try_send(line.clone()).is_err() {
                stuck.push(id);
            }
        }
        stuck
    }
}

fn serve(stream: UnixStream, id: u64, tx: SyncSender<Input>) {
    let (wtx, wrx) = mpsc::sync_channel::<String>(QUEUE);
    let Ok(mut writer) = stream.try_clone() else { return };
    let Ok(handle) = stream.try_clone() else { return };
    // An organ that stops reading is cut off rather than holding a writer forever.
    let _ = writer.set_write_timeout(Some(WRITE_TIMEOUT));
    let unit = peer::unit_of(&stream);
    if tx.send(Input::Joined(id, wtx, handle, unit)).is_err() {
        return;
    }
    // Writer: ends when the hub drops this organ, which also unblocks the reader.
    // A thread that cannot be started loses this organ, never the brainstem.
    let writer_thread = std::thread::Builder::new().spawn(move || {
        for line in wrx {
            if writer.write_all(line.as_bytes()).is_err() {
                break;
            }
        }
        let _ = writer.shutdown(Shutdown::Both);
    });
    if writer_thread.is_err() {
        let _ = tx.send(Input::Gone(id));
        return;
    }
    let reader_tx = tx.clone();
    let reader_thread = std::thread::Builder::new().spawn(move || {
        let tx = reader_tx;
        let mut reader = BufReader::new(stream);
        let mut line = Vec::new();
        loop {
            match read_line_capped(&mut reader, &mut line, MAX_LINE) {
                Ok(LineRead::Eof) | Err(_) => break,
                Ok(LineRead::TooLong) => continue,
                Ok(LineRead::Line) => {
                    let Ok(text) = std::str::from_utf8(&line) else { continue };
                    if let Some(ev) = Event::parse(text) {
                        if tx.send(Input::Line(id, ev)).is_err() {
                            break;
                        }
                    }
                }
            }
        }
        let _ = tx.send(Input::Gone(id));
    });
    if reader_thread.is_err() {
        let _ = tx.send(Input::Gone(id));
    }
}

/// Print what the hardware body finds, for the boot log's reader and for the
/// Try menu's "Check hardware". It never fails: absence is an answer too.
fn probe_hardware(root: &Path) {
    println!("brainstem: hardware probe of {}", root.display());
    for line in body::sysfs::survey(root) {
        println!("  {line}");
    }
    #[cfg(target_os = "linux")]
    if root == Path::new("/") {
        println!("  {}", linux_body::probe_lid());
    }
    let mut sensors = body::sysfs::Sensors::new(root.to_path_buf());
    let reading = sensors.pass();
    for line in sensors.notes.take().into_iter().filter(|line| line.contains("took")) {
        println!("  {line}");
    }
    println!("  reading: {}", reading.describe());
}

fn configure_body(cfg: &mut machine::Config, appliance_body: bool, fw: &Path) -> bool {
    let lab_body = appliance_body && ["serial", "loopback", "microphone"]
        .into_iter().any(|name| fw.join(name).join("raw").exists());
    if lab_body {
        // The lab CPU is emulated and takes much longer to answer.
        cfg.think_timeout = 1800.0;
        cfg.think_blind = 1800.0;
        cfg.think_stall = 300.0;
        cfg.speak_timeout = 900.0;
    }
    lab_body
}

/// A number of seconds from an option or the environment.
fn seconds(text: &str) -> Option<f64> {
    text.trim().parse::<f64>().ok().filter(|s| s.is_finite() && *s > 0.0)
}

/// Run the power-off without waiting for it: the hub must keep beating
/// while systemd stops everything (and stops it, in the end, with SIGTERM).
fn power_off(command: &str) -> std::io::Result<std::process::Child> {
    let mut words = command.split_whitespace();
    let program = words.next().ok_or_else(|| std::io::Error::other("empty power-off command"))?;
    std::process::Command::new(program)
        .args(words)
        .stdin(std::process::Stdio::null())
        .spawn()
}

fn lid_changed(
    body: &body::Body,
    machine: &mut machine::Machine,
    dream_lid: &mut Option<u64>,
    now: f64,
    pending: &mut Vec<(Event, Option<u64>)>,
) {
    let mut reading = body.lid_event();
    if dream_lid.take().is_some_and(|closed| closed.checked_add(1) == Some(body.lid_seq())) && !body.lid_blocked() {
        reading = reading.with("dreamed", true);
    }
    pending.push((reading, None));
    for event in machine.set_env(body.lid(), body.dark(), body.night(), now) {
        pending.push((event, None));
    }
}

fn blocked_by_lid(topic: &str) -> bool {
    topic_matches("hear.*", topic)
        || topic_matches("see.*", topic)
        || matches!(
            topic,
            "say.request" | "say.started" | "say.level" | "say.done"
                | "mind.emotion" | "mind.reply_done" | "mind.announce" | "being.sleep"
        )
}

/// True if the kernel command line asks for the boot messages to stay visible.
fn verbose_boot(cmdline: &str) -> bool {
    cmdline.split_whitespace().any(|word| word == "being.verbose=1")
}

fn main() {
    let mut path = bus::socket_path();
    let mut cfg = machine::Config::default();
    let mut log = std::env::var_os("BEING_LOG").is_some();
    let mut mind_dir = std::env::var_os("BEING_MIND").map(PathBuf::from);
    let mut mind_is_a_mount = false;
    let mut appliance_body = false;
    let env_seconds = |name: &str| {
        let text = std::env::var(name).ok().filter(|t| !t.trim().is_empty())?;
        let value = seconds(&text);
        if value.is_none() {
            eprintln!("brainstem: {name}={text:?} is not a number of seconds; ignored");
        }
        value
    };
    let mut think_timeout = env_seconds("BEING_THINK_TIMEOUT");
    let mut think_stall = env_seconds("BEING_THINK_STALL");
    let mut speak_timeout = None;
    let mut probe = false;
    let mut sysfs_root = PathBuf::from("/");
    let mut state_file = None;
    let mut poweroff_cmd: Option<String> = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--bus" => path = PathBuf::from(args.next().unwrap_or_else(|| die("--bus needs a path"))),
            "--require" => {
                let list = args.next().unwrap_or_else(|| die("--require needs a list"));
                cfg.required = list.split(',').filter(|s| !s.is_empty()).map(str::to_string).collect();
            }
            "--think-timeout" | "--speak-timeout" | "--think-stall" => {
                let secs = args
                    .next()
                    .as_deref()
                    .and_then(seconds)
                    .unwrap_or_else(|| die(&format!("{arg} needs a number of seconds")));
                match arg.as_str() {
                    "--think-timeout" => think_timeout = Some(secs),
                    "--think-stall" => think_stall = Some(secs),
                    _ => speak_timeout = Some(secs),
                }
            }
            "--mind-dir" => mind_dir = Some(PathBuf::from(args.next().unwrap_or_else(|| die("--mind-dir needs a path")))),
            "--mind-is-a-mount" => mind_is_a_mount = true,
            "--appliance-body" => appliance_body = true,
            "--state-file" => state_file = Some(PathBuf::from(args.next().unwrap_or_else(|| die("--state-file needs a path")))),
            "--poweroff-cmd" => poweroff_cmd = Some(args.next().unwrap_or_else(|| die("--poweroff-cmd needs a command"))),
            "--log" => log = true,
            "--probe-hardware" => probe = true,
            "--sysfs-root" => sysfs_root = PathBuf::from(args.next().unwrap_or_else(|| die("--sysfs-root needs a path"))),
            "-h" | "--help" => {
                println!("{HELP}");
                return;
            }
            other => die(&format!("unknown option {other}\n\n{HELP}")),
        }
    }

    if probe {
        probe_hardware(&sysfs_root);
        return;
    }

    let fw = Path::new("/sys/firmware/qemu_fw_cfg/by_name/opt/beingos");
    let lab_body = configure_body(&mut cfg, appliance_body, fw);
    if let Some(cap) = think_timeout {
        // An explicit cap is the owner's (or the lab's) choice for every mind.
        cfg.think_timeout = cap;
        cfg.think_blind = cap;
    }
    cfg.think_stall = think_stall.unwrap_or(cfg.think_stall);
    cfg.speak_timeout = speak_timeout.unwrap_or(cfg.speak_timeout);
    if appliance_body {
        let profile = if lab_body { "QEMU lab" } else { "standard" };
        // lab/tests/test_l5.py waits for this line exactly.
        eprintln!("brainstem: {profile} timeouts, think {}s, speak {}s", cfg.think_timeout, cfg.speak_timeout);
        eprintln!(
            "brainstem: a reply is given up after {}s with no progress, or {}s from a mind that reports none",
            cfg.think_stall, cfg.think_blind
        );
    }

    if UnixStream::connect(&path).is_ok() {
        die(&format!("another brainstem is already listening on {}", path.display()));
    }
    if let Some(dir) = path.parent() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            die(&format!("{}: {e}", dir.display()));
        }
    }
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).unwrap_or_else(|e| die(&format!("{}: {e}", path.display())));
    eprintln!("brainstem: listening on {}", path.display());

    let (tx, rx) = mpsc::sync_channel::<Input>(INPUTS);
    std::thread::spawn(move || {
        let mut next_id = 0u64;
        let mut complained = false;
        loop {
            match listener.accept() {
                Ok((stream, _)) => {
                    complained = false;
                    next_id += 1;
                    serve(stream, next_id, tx.clone());
                }
                Err(e) => {
                    // Out of descriptors, say: never spin on it.
                    if !complained {
                        complained = true;
                        eprintln!("brainstem: accept failed: {e}");
                    }
                    std::thread::sleep(ACCEPT_BACKOFF);
                }
            }
        }
    });

    // SAFETY: the handler only stores to an atomic.
    unsafe {
        libc::signal(libc::SIGTERM, on_stop as extern "C" fn(libc::c_int) as libc::sighandler_t);
        libc::signal(libc::SIGINT, on_stop as extern "C" fn(libc::c_int) as libc::sighandler_t);
    }
    let boot = life::boot_id();
    let body_name = life::body();
    let mut life: Option<life::Life> = None;
    let mut life_next_try = 0.0f64;
    let mut life_complained = false;

    // Names are checked against units only on the appliance (see peer.rs).
    let enforce_units = peer::enforcing();
    let mut refused_said: HashSet<String> = HashSet::new();

    let started = Instant::now();
    let mut hub = Hub { peers: HashMap::new(), log, started };
    let state_file = state_file.unwrap_or_else(|| stem_state::default_path(&path));
    let saved = stem_state::load(&state_file, &boot);
    let mut machine = match &saved {
        Some(saved) => {
            let r = &saved.resume;
            eprintln!(
                "brainstem: resuming after a restart: {}{}, lid reading {}",
                r.coarse.name(),
                if r.muted { ", muted" } else { "" },
                saved.lid_seq
            );
            machine::Machine::resume(cfg, 0.0, r)
        }
        None => machine::Machine::new(cfg, 0.0),
    };
    let mut camera = camera::Camera::default();
    let hardware_body = cfg!(target_os = "linux") && appliance_body && !lab_body;
    let mut body = if hardware_body { body::Body::physical(0.0) } else { body::Body::new(0.0) };
    let (thresholds, threshold_notes) = battery::Thresholds::from_env(|name| std::env::var(name).ok());
    for note in threshold_notes {
        eprintln!("brainstem: {note}");
    }
    body.care = battery::Battery::new(thresholds);
    // Battery power-offs are the hardware body's, or a test's that names a command.
    let power_control = hardware_body || poweroff_cmd.is_some();
    // Elsewhere (the lab, the simulator) no goodbye is announced for a
    // power-off that will never come; the pleas are still made.
    body.care.can_power_off = power_control;
    let poweroff_cmd = poweroff_cmd.unwrap_or_else(|| "systemctl poweroff".to_string());
    let mut powering_off: Option<std::process::Child> = None;
    if let Some(saved) = &saved {
        body.continue_lid_seq(saved.lid_seq);
    }
    let mut last_saved = saved;
    let mut state_complained = false;
    let mut dream_lid = None;
    machine.set_env(body.lid(), body.dark(), body.night(), 0.0);
    #[cfg(target_os = "linux")]
    let mut hardware = hardware_body.then(|| (linux_body::Lid::new(), linux_body::Backlight::new()));
    // Battery, temperature and light are slow to read on real hardware, so
    // they arrive from a thread of their own and the hub never waits on them.
    #[cfg(target_os = "linux")]
    let mut sensors = if hardware_body {
        body::sysfs::spawn(PathBuf::from("/"), Duration::from_secs(2)).map(|rx| body::sysfs::Feed::new(rx, 0.0))
    } else {
        None
    };
    #[cfg(target_os = "linux")]
    let mut panel_error_reported = false;
    // On a "show boot messages" boot the panel stays lit until the face or
    // menu has drawn once, so the boot text can be read while they wait.
    #[cfg(target_os = "linux")]
    let mut hold_lit = std::fs::read_to_string("/proc/cmdline")
        .is_ok_and(|line| verbose_boot(&line));
    let mut last_beat = 0.0f64;
    let mut last_state = machine.state();
    let mut watchdog = bus::notify::Notifier::from_env();
    // The panel's brightness is the hardware body's; elsewhere there is none.
    let mut brightness = if hardware_body {
        brightness::Brightness::discover(Path::new("/"))
    } else {
        brightness::Brightness::none()
    };
    if let Some(name) = brightness.name() {
        let level = brightness.level().map_or("unknown".to_string(), |l| format!("{l}%"));
        eprintln!("brainstem: brightness through {name}, now {level}");
    }
    let mut next_tick = started + TICK_INTERVAL;

    while let Some(input) = next_input(&rx, next_tick) {
        let now = started.elapsed().as_secs_f64();
        if STOP.load(Ordering::SeqCst) {
            watchdog.stopping();
            if let Err(e) = brightness.restore() {
                eprintln!("brainstem: could not put the brightness back: {e}");
            }
            // Going to sleep properly: the next wake will know it was not a power cut.
            if let Some(life) = &life {
                if let Err(e) = life.sleep(life::unix_now()) {
                    eprintln!("brainstem: could not record going to sleep: {e}");
                }
            }
            let _ = std::fs::remove_file(&path);
            eprintln!("brainstem: asleep");
            return;
        }
        // Events to send, each with the organ it came from (None: the brainstem).
        let mut pending: Vec<(Event, Option<u64>)> = Vec::new();
        let mut gone: Vec<u64> = Vec::new();
        let tick = matches!(&input, Input::Tick);
        #[cfg(target_os = "linux")]
        let was_blank = body.panel_blank();

        match input {
            Input::Joined(id, tx, stream, unit) => {
                hub.peers.insert(id, Peer { name: None, subs: Vec::new(), tx, stream, unit });
            }
            Input::Line(id, ev) => {
                if ev.topic == "hello" {
                    let name = ev.str("name").unwrap_or("unknown").to_string();
                    let unit = hub.peers.get(&id).and_then(|p| p.unit.clone());
                    if enforce_units && !peer::may_claim(&name, unit.as_deref()) {
                        // Never announced, never routed: the connection ends here.
                        if refused_said.len() < 64 && refused_said.insert(format!("{name}/{unit:?}")) {
                            eprintln!(
                                "brainstem: refused \"{name}\" from {}: that name belongs to another unit",
                                unit.as_deref().unwrap_or("an unknown process")
                            );
                        }
                        gone.push(id);
                    } else {
                        let subs: Vec<String> = ev
                            .data
                            .get("sub")
                            .and_then(Value::as_array)
                            .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                            .unwrap_or_default();
                        // A restarted organ replaces its old self.
                        gone.extend(hub.peers.iter().filter(|(&i, p)| i != id && p.name.as_deref() == Some(&name)).map(|(&i, _)| i));
                        if let Some(peer) = hub.peers.get_mut(&id) {
                            pending.extend(camera.identified(id, &name, body.lid_seq()).into_iter().map(|e| (e, None)));
                            // Tell the newcomer where things stand before anything else.
                            let mut snaps = vec![
                                body.lid_event().with("snapshot", true),
                                machine.snapshot(),
                                camera.policy(),
                                body.senses(),
                                body.snapshot(),
                            ];
                            snaps.extend(life.as_ref().map(|l| l.event()));
                            for snap in snaps {
                                if subs.iter().any(|s| topic_matches(s, &snap.topic)) {
                                    let _ = peer.tx.try_send(snap.to_line());
                                }
                            }
                            // The receipt comes last, so a client that waits for it has its snapshots.
                            if ev.bool("ack") == Some(true) {
                                let _ = peer.tx.try_send(Event::new("hello.ok").with("name", name.as_str()).to_line());
                            }
                            peer.name = Some(name.clone());
                            peer.subs = subs;
                        }
                        let up = Event::new("organ.up").with("name", name);
                        pending.extend(machine.on_event(&up, now).into_iter().map(|e| (e, None)));
                        pending.insert(0, (up, Some(id)));
                    }
                } else if let Some(from) = hub.peers.get(&id).and_then(|p| p.name.clone()) {
                    // Nothing is routed for a connection that has not said hello.
                    let from_face = from == "face";
                    let seq = body.lid_seq();
                    if allowed_from(&ev.topic, &from, hardware_body) && mind_may_press(&ev, &from, hardware_body) {
                        match ev.topic.as_str() {
                            "panel.ready" => {
                                if from_face {
                                    if let Some(seq) = ev.data.get("seq").and_then(Value::as_u64) {
                                        body.panel_ready(seq);
                                        pending.push((ev, Some(id)));
                                    }
                                }
                            }
                            "key.panel_recover" => {
                                if from_face {
                                    body.recover_panel();
                                }
                            }
                            "sim.body" if hardware_body => {}
                            "key.touch" if !from_face => {}
                            "camera.request" => camera.request(id, &ev),
                            "see.presence" if !body.lid_blocked() => {
                                pending.extend(camera.observe(id, &ev, seq, now).into_iter().map(|e| (e, None)));
                            }
                            topic if topic_matches("see.*", topic) || topic_matches("camera.*", topic) => {}
                            "mind.clock" => {
                                body.set_utc_offset(ev.data.get("utc_offset").and_then(Value::as_i64));
                            }
                            "mind.shutdown_ready" => body.mind_saved(),
                            "being.brightness" => pending.push((brightness.request(&ev), None)),
                            "mind.dream_committed" => {
                                if body.lid_confirmed_closed()
                                    && machine.state() == machine::State::Asleep
                                    && ev.data.get("lid_seq").and_then(Value::as_u64) == Some(seq)
                                {
                                    dream_lid = Some(seq);
                                }
                            }
                            "say.done" if ev.data.contains_key("announcement") && from != "voice" => {}
                            topic if blocked_by_lid(topic)
                                && (body.lid_blocked()
                                    || ev.data.get("lid_seq").is_some_and(|value| value.as_u64() != Some(seq))) => {}
                            _ => {
                                body.on_event(&ev, now);
                                let outs = machine.on_event(&ev, now);
                                pending.push((ev, Some(id)));
                                pending.extend(outs.into_iter().map(|e| (e, None)));
                            }
                        }
                    }
                    if seq != body.lid_seq() {
                        lid_changed(&body, &mut machine, &mut dream_lid, now, &mut pending);
                    }
                }
            }
            Input::Gone(id) => gone.push(id),
            Input::Tick => {
                #[cfg(target_os = "linux")]
                if let Some((lid, _)) = &mut hardware {
                    for reading in lid.poll() {
                        let seq = body.lid_seq();
                        body.set_lid(reading);
                        if seq != body.lid_seq() {
                            lid_changed(&body, &mut machine, &mut dream_lid, now, &mut pending);
                        }
                    }
                }
                #[cfg(target_os = "linux")]
                if let Some(feed) = &mut sensors {
                    if let Some(line) = feed.update(&mut body, now) {
                        eprintln!("brainstem: {line}");
                    }
                }
                // Wake into the life record as soon as the mind partition is there.
                if let (None, Some(dir)) = (&life, &mind_dir) {
                    if now >= life_next_try {
                        life_next_try = now + 2.0;
                        let result = if mind_is_a_mount && !is_mount_point(dir) {
                            Err(std::io::Error::other(format!("{} is not mounted", dir.display())))
                        } else {
                            life::Life::wake_at(dir, life::unix_now(), life::boot_clock(), &boot, &body_name)
                        };
                        match result {
                            Ok(woke) => {
                                eprintln!(
                                    "brainstem: wake {} in {}{}{}{}",
                                    woke.wakes,
                                    woke.body,
                                    if woke.first { ", born just now" } else { "" },
                                    if woke.lost_power { ", after losing power" } else { "" },
                                    if woke.new_body { ", in a new body" } else { "" },
                                );
                                pending.push((woke.event(), None));
                                life = Some(woke);
                            }
                            Err(e) => {
                                if !life_complained {
                                    life_complained = true;
                                    eprintln!("brainstem: no life record yet: {e}");
                                }
                                if e.kind() == std::io::ErrorKind::InvalidData {
                                    // Damaged, not missing. Never overwrite it with a new life.
                                    mind_dir = None;
                                }
                            }
                        }
                    }
                }
                if let Some(life) = &life {
                    body.note = life.senses_at(life::unix_now(), life::boot_clock());
                }
                let mut outs = machine.set_env(body.lid(), body.dark(), body.night(), now);
                outs.extend(machine.on_tick(now));
                outs.extend(body.tick(now));
                if body.take_power_off() {
                    let why = body.care.shutting_down().unwrap_or("battery");
                    eprintln!("brainstem: powering off ({why}) with {poweroff_cmd:?}");
                    match power_off(&poweroff_cmd) {
                        Ok(child) => powering_off = Some(child),
                        Err(e) => {
                            eprintln!("brainstem: could not power off: {e}; still watching the battery");
                            body.care.power_off_failed(now);
                        }
                    }
                }
                if let Some(child) = &mut powering_off {
                    // Reap it; systemd will stop the brainstem in its own time.
                    if let Ok(Some(status)) = child.try_wait() {
                        if !status.success() {
                            eprintln!("brainstem: the power-off command failed: {status}; still watching the battery");
                            body.care.power_off_failed(now);
                        }
                        powering_off = None;
                    }
                }
                watchdog.kick();
                if now - last_beat >= 0.5 {
                    last_beat = now;
                    outs.push(
                        Event::new("stem.beat")
                            .with("state", machine.state().name())
                            .with("up", hub.peers.values().filter(|p| p.name.is_some()).count()),
                    );
                }
                pending.extend(outs.into_iter().map(|e| (e, None)));
            }
        }

        pending.extend(camera.update(machine.state(), body.lid_blocked(), body.lid_seq(), now)
            .into_iter().map(|e| (e, None)));

        #[cfg(target_os = "linux")]
        if let Some((_, panel)) = &mut hardware {
            if !body.panel_blank() {
                hold_lit = false;
            }
            if !hold_lit && (tick || body.panel_blank() != was_blank) {
                match panel.set_blank(body.panel_blank()) {
                    Ok(()) => panel_error_reported = false,
                    Err(error) if !panel_error_reported => {
                        eprintln!("brainstem: backlight: {error}");
                        panel_error_reported = true;
                    }
                    Err(_) => {}
                }
            }
        }

        // Send, and keep going until organs lost along the way are accounted for.
        loop {
            for (ev, from) in pending.drain(..) {
                gone.extend(hub.route(&ev, from));
            }
            let Some(id) = gone.pop() else { break };
            if let Some(peer) = hub.peers.remove(&id) {
                pending.extend(camera.disconnected(id, body.lid_seq()).into_iter().map(|e| (e, None)));
                if let Some(name) = peer.name.clone() {
                    // Only report it down if no newer connection took the name.
                    if !hub.peers.values().any(|p| p.name.as_deref() == Some(&name)) {
                        let down = Event::new("organ.down").with("name", name);
                        let outs = machine.on_event(&down, now);
                        pending.push((down, None));
                        pending.extend(outs.into_iter().map(|e| (e, None)));
                    }
                }
                // Dropping the peer shuts its socket, which ends its threads.
                drop(peer);
            }
        }

        if machine.state() != last_state {
            eprintln!("brainstem: {} -> {}", last_state.name(), machine.state().name());
            last_state = machine.state();
        }
        // Write down what a restart would need, whenever it changes.
        let now_saved = stem_state::Saved { resume: machine.persist(), lid_seq: body.lid_seq() };
        if last_saved != Some(now_saved) {
            match stem_state::save(&state_file, &now_saved, &boot) {
                Ok(()) => {
                    last_saved = Some(now_saved);
                    state_complained = false;
                }
                Err(e) if !state_complained => {
                    state_complained = true;
                    eprintln!("brainstem: could not write {}: {e}", state_file.display());
                }
                Err(_) => {}
            }
        }
        if tick {
            next_tick = next_tick_after(next_tick, Instant::now());
        }
    }
}

fn die(msg: &str) -> ! {
    eprintln!("brainstem: {msg}");
    std::process::exit(2);
}

#[cfg(test)]
#[test]
fn lab_timeouts_require_appliance_mode_and_a_firmware_marker() {
    let fw = std::env::temp_dir().join(format!("being-brainstem-fw-{}", std::process::id()));
    std::fs::create_dir(&fw).unwrap();
    let profile = |appliance_body| {
        let mut cfg = machine::Config::default();
        let lab_body = configure_body(&mut cfg, appliance_body, &fw);
        (lab_body, cfg.think_timeout, cfg.speak_timeout)
    };
    let standard = (false, 600.0, 60.0);
    assert_eq!(profile(true), standard);
    for name in ["serial", "loopback", "microphone"] {
        let marker = fw.join(name);
        std::fs::create_dir(&marker).unwrap();
        assert_eq!(profile(true), standard);
        std::fs::write(marker.join("raw"), []).unwrap();
        assert_eq!(profile(true), (true, 1800.0, 900.0));
        assert_eq!(profile(false), standard);
        std::fs::remove_dir_all(marker).unwrap();
    }
    std::fs::remove_dir(fw).unwrap();
}

#[cfg(test)]
#[test]
fn the_voice_takes_orders_only_from_whoever_owns_each_one() {
    assert!(!allowed_from("voice.mute", "mind", false), "mute is the brainstem's");
    assert!(allowed_from("voice.volume", "mind", true), "the owner's louder, through the mind");
    assert!(!allowed_from("voice.volume", "eyes", false));
    assert!(allowed_from("voice.chirps", "mind", true));
    assert!(allowed_from("voice.volume_state", "voice", true));
    assert!(!allowed_from("voice.volume_state", "mind", true));
    assert!(!allowed_from("voice.anything_new", "mind", true));
    assert!(allowed_from("body.care", "bodycare", true) && !allowed_from("body.care", "mind", true));
    assert!(!allowed_from("key.mute", "voice", true) && allowed_from("key.mute", "voice", false));
}

#[cfg(test)]
#[test]
fn wave_two_topics_are_allowed_only_from_their_owners() {
    for hardware in [true, false] {
        assert!(allowed_from("voice.level", "voice", hardware));
        assert!(!allowed_from("voice.level", "mind", hardware), "levels are measured by the voice");
        for topic in ["voice.level_request", "voice.engine"] {
            assert!(allowed_from(topic, "mind", hardware), "{topic}");
            assert!(!allowed_from(topic, "voice", hardware) && !allowed_from(topic, "hands", hardware), "{topic}");
        }
        assert!(allowed_from("being.brightness", "mind", hardware));
        assert!(!allowed_from("being.brightness", "face", hardware) && !allowed_from("being.brightness", "hands", hardware));
        assert!(allowed_from("key.attend", "face", hardware));
        assert!(!allowed_from("body.brightness", "mind", hardware), "body.* is the brainstem's");
        assert!(!allowed_from("body.state", "bodycare", hardware));
    }
    assert!(!allowed_from("key.attend", "mind", true), "on the laptop only the face presses keys");
    assert!(allowed_from("key.mute", "mind", true), "\"stop listening\"");
    let mute = |on: Option<bool>| match on {
        Some(on) => Event::new("key.mute").with("on", on),
        None => Event::new("key.mute"),
    };
    assert!(mind_may_press(&mute(Some(true)), "mind", true));
    assert!(!mind_may_press(&mute(Some(false)), "mind", true), "the mind never reopens the ears");
    assert!(!mind_may_press(&mute(None), "mind", true), "nor toggles them");
    assert!(mind_may_press(&mute(None), "face", true), "the M key still toggles");
    assert!(mind_may_press(&mute(None), "mind", false), "the lab's console stands in for the keyboard");
    assert!(allowed_from("key.attend", "console", false), "the lab's typed console stands in");
    assert!(allowed_from("key.interrupt", "keyboard", true), "typing over Pip stops it, as Esc does");
    // The owner's words come from the voice, or from the typing chat.
    for topic in ["hear.wake", "hear.speech_start", "hear.partial", "hear.final", "hear.cancel"] {
        assert!(allowed_from(topic, "voice", true), "{topic}");
        assert!(allowed_from(topic, "keyboard", true), "{topic}");
        for organ in ["eyes", "hands", "bodycare", "mind", "face", "rogue"] {
            assert!(!allowed_from(topic, organ, true), "{organ} may not send {topic} on the laptop");
        }
        assert!(allowed_from(topic, "console", false), "the lab's console stands in");
    }
    for topic in ["hear.sound", "hear.missed", "hear.anything_new"] {
        assert!(allowed_from(topic, "voice", true) && !allowed_from(topic, "keyboard", true), "{topic}");
    }
    assert!(allowed_from("see.presence", "eyes", true) && !allowed_from("see.presence", "keyboard", true));
    assert!(!allowed_from("see.presence", "rogue", false), "the camera is the eyes' even in the lab");
    assert!(!allowed_from("key.mute", "keyboard", true) && !allowed_from("key.attend", "keyboard", true));
}

#[cfg(test)]
mod tick_tests {
    #[test]
    fn verbose_boot_is_read_from_the_command_line() {
        assert!(super::verbose_boot("quiet being.try=1 being.verbose=1 panic=0"));
        assert!(!super::verbose_boot("quiet being.try=1 being.verbose=10"));
        assert!(!super::verbose_boot(""));
    }

    use super::*;

    #[test]
    fn an_overdue_tick_takes_priority_without_draining_organ_input() {
        let (tx, rx) = mpsc::channel();
        tx.send(Input::Gone(1)).unwrap();
        tx.send(Input::Gone(2)).unwrap();
        assert!(matches!(next_input(&rx, Instant::now()), Some(Input::Tick)));
        assert!(matches!(rx.try_recv(), Ok(Input::Gone(1))));
        assert!(matches!(rx.try_recv(), Ok(Input::Gone(2))));
    }

    #[test]
    fn only_an_overrun_shifts_the_next_period() {
        let deadline = Instant::now();
        let normal_finish = deadline + TICK_INTERVAL / 2;
        assert_eq!(next_tick_after(deadline, normal_finish), deadline + TICK_INTERVAL);

        let late_finish = deadline + TICK_INTERVAL * 3;
        let next_tick = next_tick_after(deadline, late_finish);
        assert_eq!(next_tick, late_finish + TICK_INTERVAL);
        let (tx, rx) = mpsc::channel();
        tx.send(Input::Gone(1)).unwrap();
        assert!(matches!(next_input(&rx, next_tick), Some(Input::Gone(1))));
    }
}

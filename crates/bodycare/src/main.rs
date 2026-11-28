//! being-bodycare: the laptop's power and thermal keeper. See lib.rs.

use bodycare::care::{Care, Mode};
use bodycare::journal::Journal;
use bodycare::policy::{Being, Config};
use bus::{Client, Event};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

const HELP: &str = "\
being-bodycare: one fail-safe owner of the charge limit, fan floor and CPU caps

  being-bodycare                    run (observe mode: decide and report, write nothing)
  being-bodycare --mode act         run and write (or BEING_BODYCARE_MODE=act)
  being-bodycare --charge-limit N   keep the battery at N% (60-100; 100 lifts the cap;
                                    or BEING_CHARGE_LIMIT). Unset: never touched
  being-bodycare --dream-target C   hold the package at C while dreaming (or
                                    BEING_DREAM_TARGET_C; default 80)
  being-bodycare --restore          put back every control it changed, but keep the
                                    owner's charge limit (systemd ExecStopPost)
  being-bodycare --restore --charge-limit-too
                                    undo the charge limit as well
  being-bodycare --once             one pass, print the report, exit (writes in act mode)
  being-bodycare --bus PATH         the bus socket (default as for every organ)
  being-bodycare --root PATH        a copy of / to work on instead (tests)
  being-bodycare --journal PATH     the restore journal (default: bodycare-restore.json in
                                    systemd's $RUNTIME_DIRECTORY, else /run/being/)";

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_stop(_signal: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

fn die(msg: &str) -> ! {
    eprintln!("bodycare: {msg}");
    std::process::exit(2);
}

/// Topics bodycare listens to. `mind.dreaming` (`active`) is sent by the mind
/// while the dream cortex works.
const SUBS: &[&str] = &["state.changed", "body.lid", "mind.dreaming", "organ.down"];
/// One pass a second for the cheap readings (fan, package, adapter); the
/// battery's own files are read far less often (`policy::battery_due`).
const PERIOD: Duration = Duration::from_secs(1);
const STOP_CHECK: Duration = Duration::from_millis(500);
const REPORT_EVERY: f64 = 10.0;
const RECONNECT: Duration = Duration::from_secs(2);
/// With the bus gone this long, the being's state is unknown.
const BUS_LOST: f64 = 5.0;

fn main() {
    let mut root = PathBuf::from("/");
    let mut journal = default_journal(std::env::var_os("RUNTIME_DIRECTORY"));
    let mut bus_path = bus::socket_path();
    let mut cfg = Config::default();
    let mut mode = Mode::Observe;
    let mut restore = false;
    let mut charge_limit_too = false;
    let mut once = false;
    let mut notes = Vec::new();

    if let Ok(text) = std::env::var("BEING_BODYCARE_MODE") {
        match Mode::from_name(&text) {
            Some(m) => mode = m,
            None if text.trim().is_empty() => {}
            None => notes.push(format!("BEING_BODYCARE_MODE={text:?} is not observe or act; observing")),
        }
    }
    let parse_limit = |text: &str| text.trim().parse::<i64>().ok().filter(|v| (60..=100).contains(v));
    if let Ok(text) = std::env::var("BEING_CHARGE_LIMIT") {
        if !text.trim().is_empty() {
            match parse_limit(&text) {
                Some(v) => cfg.charge_limit = Some(v),
                None => notes.push(format!("BEING_CHARGE_LIMIT={text:?} is not 60-100; the charge limit is left alone")),
            }
        }
    }
    let parse_target = |text: &str| text.trim().parse::<f64>().ok().filter(|v| (50.0..=90.0).contains(v));
    if let Ok(text) = std::env::var("BEING_DREAM_TARGET_C") {
        if !text.trim().is_empty() {
            match parse_target(&text) {
                Some(v) => cfg.dream_target_c = v,
                None => notes.push(format!("BEING_DREAM_TARGET_C={text:?} is not 50-90; using {}", cfg.dream_target_c)),
            }
        }
    }

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().unwrap_or_else(|| die(&format!("{arg} needs {what}")));
        match arg.as_str() {
            "--mode" => mode = Mode::from_name(&value("observe or act")).unwrap_or_else(|| die("--mode is observe or act")),
            "--charge-limit" => cfg.charge_limit = Some(parse_limit(&value("a percentage")).unwrap_or_else(|| die("--charge-limit is 60-100"))),
            "--dream-target" => cfg.dream_target_c = parse_target(&value("degrees")).unwrap_or_else(|| die("--dream-target is 50-90")),
            "--restore" => restore = true,
            "--charge-limit-too" => charge_limit_too = true,
            "--once" => once = true,
            "--bus" => bus_path = PathBuf::from(value("a path")),
            "--root" => root = PathBuf::from(value("a path")),
            "--journal" => journal = PathBuf::from(value("a path")),
            "-h" | "--help" => {
                println!("{HELP}");
                return;
            }
            other => die(&format!("unknown option {other}\n\n{HELP}")),
        }
    }
    for note in notes {
        eprintln!("bodycare: {note}");
    }

    if restore {
        let mut j = Journal::open(&journal);
        if j.is_empty() {
            eprintln!("bodycare: nothing to restore");
        }
        for line in j.restore(charge_limit_too) {
            eprintln!("bodycare: {line}");
        }
        return;
    }

    let mut care = Care::new(&root, &journal, cfg, mode);
    eprintln!(
        "bodycare: {} mode{}",
        mode.name(),
        care.cfg.charge_limit.map_or(String::new(), |l| format!(", charge limit {l}%"))
    );
    if once {
        care.step(&Being::default(), 0.0);
        for line in care.notes.drain(..) {
            eprintln!("bodycare: {line}");
        }
        println!("{}", care.report().to_line().trim_end());
        return;
    }

    // SAFETY: the handler only stores to an atomic.
    unsafe {
        libc::signal(libc::SIGTERM, on_stop as extern "C" fn(libc::c_int) as libc::sighandler_t);
        libc::signal(libc::SIGINT, on_stop as extern "C" fn(libc::c_int) as libc::sighandler_t);
    }
    let mut notifier = bus::notify::Notifier::from_env();
    let started = Instant::now();
    let mut client: Option<Client> = None;
    let mut next_connect = started;
    let mut bus_seen = 0.0f64;
    let mut being = Being::default();
    let mut last_report: Option<(f64, String)> = None;
    let mut ready = false;

    while !STOP.load(Ordering::SeqCst) {
        let now = started.elapsed().as_secs_f64();
        if client.is_none() && Instant::now() >= next_connect {
            next_connect = Instant::now() + RECONNECT;
            client = Client::connect(&bus_path, "bodycare", SUBS).ok();
            // A profile from an earlier connection is never authority for a
            // new brainstem. Wait for this connection's state snapshot.
            if client.is_some() {
                being.state_fresh = false;
                being.state_epoch = None;
            }
        }
        let mut lost = false;
        if let Some(c) = &client {
            loop {
                match c.try_recv() {
                    Ok(Some(ev)) => {
                        bus_seen = now;
                        take(&mut being, &ev);
                    }
                    Ok(None) => break,
                    Err(_) => {
                        lost = true;
                        break;
                    }
                }
            }
            // A connection counts as heard from while it is up.
            if !lost {
                bus_seen = now;
            }
        }
        if lost {
            client = None;
        }
        if now - bus_seen > BUS_LOST {
            // Blind: no talking boost, no dreams, the lid unknown.
            being = Being::default();
        }

        care.step(&being, now);
        for line in care.notes.drain(..) {
            eprintln!("bodycare: {line}");
        }
        let report = care.report();
        // Publish on a change of what matters, and every ten seconds.
        let key = {
            let mut quiet = report.clone();
            for k in ["package_c", "battery_c", "fan_rpm", "cpu_busy"] {
                quiet.data.remove(k);
            }
            quiet.to_line()
        };
        let due = last_report.as_ref().is_none_or(|(at, last)| now - at >= REPORT_EVERY || *last != key);
        if due {
            if let Some(c) = &mut client {
                if c.send(&report).is_err() {
                    client = None;
                }
            }
            notifier.status(&format!(
                "{} mode; cpu {}; fan floor {}",
                care.mode.name(),
                report.data.get("cpu_reasons").map_or("?".into(), |v| v.to_string()),
                report.data.get("fan_floor").map_or("?".into(), |v| v.to_string()),
            ));
            last_report = Some((now, key));
        }
        if !ready {
            ready = true;
            notifier.ready();
        }
        notifier.kick();
        // Sleep in pieces so a stop is answered within half a second, but
        // without waking the CPU ten times a second for nothing.
        let until = Instant::now() + PERIOD;
        while !STOP.load(Ordering::SeqCst) {
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            std::thread::sleep(left.min(STOP_CHECK));
        }
    }

    notifier.stopping();
    for line in care.restore() {
        eprintln!("bodycare: {line}");
    }
    eprintln!("bodycare: stopped");
}

/// The restore journal's home. Under ProtectSystem=strict only a directory
/// the unit owns (RuntimeDirectory=, handed over as $RUNTIME_DIRECTORY, the
/// first of a colon-separated list) or one named in ReadWritePaths= can be
/// written; /run/being belongs to the brainstem.
fn default_journal(runtime_dir: Option<std::ffi::OsString>) -> PathBuf {
    const NAME: &str = "bodycare-restore.json";
    let first = runtime_dir.and_then(|dirs| {
        let dirs = dirs.to_string_lossy().into_owned();
        dirs.split(':').next().filter(|d| d.starts_with('/')).map(PathBuf::from)
    });
    first.unwrap_or_else(|| PathBuf::from("/run/being")).join(NAME)
}

/// Take in one bus event.
fn take(being: &mut Being, ev: &Event) {
    match ev.topic.as_str() {
        "state.changed" => {
            if let (Some(state), Some(epoch)) = (ev.str("state"), ev.data.get("state_epoch").and_then(serde_json::Value::as_u64)) {
                being.state = Some(state.to_string());
                being.state_fresh = true;
                being.state_epoch = Some(epoch);
                if state != "asleep" {
                    being.dreaming = false;
                }
            }
        }
        "body.lid" => {
            being.lid_closed = ev.data.get("closed").and_then(serde_json::Value::as_bool);
            if being.lid_closed != Some(true) {
                being.dreaming = false;
            }
        }
        "mind.dreaming" => being.dreaming = ev.bool("active") == Some(true),
        "organ.down" if ev.str("name") == Some("mind") => being.dreaming = false,
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_being_is_followed_from_the_bus() {
        let mut b = Being::default();
        take(&mut b, &Event::new("state.changed").with("state", "asleep").with("state_epoch", 1));
        take(&mut b, &Event::new("body.lid").with("closed", true));
        take(&mut b, &Event::new("mind.dreaming").with("active", true));
        assert!(b.dreaming && b.asleep());
        take(&mut b, &Event::new("organ.down").with("name", "mind"));
        assert!(!b.dreaming, "a dead mind is not dreaming");
        take(&mut b, &Event::new("mind.dreaming").with("active", true));
        take(&mut b, &Event::new("body.lid").with("closed", serde_json::Value::Null));
        assert!(!b.dreaming && b.lid_closed.is_none());
        take(&mut b, &Event::new("mind.dreaming").with("active", true));
        take(&mut b, &Event::new("state.changed").with("state", "waking").with("state_epoch", 2));
        assert!(!b.dreaming);
    }

    #[test]
    fn the_journal_lives_where_the_unit_may_write() {
        assert_eq!(default_journal(None), PathBuf::from("/run/being/bodycare-restore.json"));
        assert_eq!(
            default_journal(Some("/run/being-bodycare".into())),
            PathBuf::from("/run/being-bodycare/bodycare-restore.json")
        );
        assert_eq!(
            default_journal(Some("/run/being-bodycare:/run/other".into())),
            PathBuf::from("/run/being-bodycare/bodycare-restore.json")
        );
        assert_eq!(default_journal(Some("".into())), PathBuf::from("/run/being/bodycare-restore.json"));
    }

    #[test]
    fn night_work_is_seen_without_a_dreaming_event() {
        // The mind sends no `mind.dreaming`: asleep with the lid closed is
        // enough for the dream loop to hold the package at the target.
        let mut b = Being::default();
        take(&mut b, &Event::new("state.changed").with("state", "asleep").with("state_epoch", 1));
        take(&mut b, &Event::new("body.lid").with("closed", true));
        assert!(!b.dreaming && b.night());
        take(&mut b, &Event::new("body.lid").with("closed", false));
        assert!(!b.night());
    }
}

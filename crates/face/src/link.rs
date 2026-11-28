//! The face's connection to the brainstem. It reconnects by itself, and it tells
//! the animation when the brainstem has gone quiet.

use crate::anim::{Anim, Drives};
use crate::dock::Dock;
use crate::keys::LevelBar;
use crate::look::Prefs;
use crate::options::Options;
use crate::overlay::Captions;
use crate::widgets::Readings;
use crate::quick_menu::{Menu, Position, Selection};
use bus::{Client, Event};
use sdl2::keyboard::Keycode;
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// Words heard and said (hear.*, say.request) arrive for the captions
/// overlay; it drops them unless it is open, and the face never logs them.
const SUBS: &[&str] = &[
    "state.changed",
    "face.*",
    "say.level",
    "say.stop",
    "say.done",
    "say.request",
    // Which clauses of a model reply really played: captions follow it (no words).
    "say.played",
    "stem.beat",
    // Sustained heat shows as a tired look (the brainstem names the pressure).
    "stem.budget",
    "body.lid",
    "body.state",
    "body.plug",
    "body.drives",
    "camera.policy",
    "life.woke",
    "organ.*",
    "hear.partial",
    "hear.final",
    "hear.cancel",
    // Words heard but not made out: a short squint (carries no words).
    "hear.missed",
    "mind.cancel",
    "mind.reply_done",
    "mind.route",
    "app.privacy",
    // What the brightness, volume and keyboard-light keys (or the owner's
    // words) really set, for the level bar (numbers only).
    "body.brightness",
    "body.kbd_light",
    "voice.volume_state",
    "screen.prefs",
    "screen.quick_menu",
    "screen.online",
    "screen.night",
    "app.refused",
    // How Pip is talked to, and what the typing bridge tells the owner.
    "chat.mode",
    "chat.notice",
];
const RETRY: Duration = Duration::from_millis(700);
const MENU_DISMISSED: &str = "shell.quick_menu.dismissed";
/// With no heartbeat for this long, the face looks unwell.
const QUIET: Duration = Duration::from_secs(3);
// Body samples arrive on change and at least every five seconds.
const READINGS_MAX_AGE: Duration = Duration::from_secs(15);
const ONLINE_MAX_AGE: Duration = Duration::from_secs(3);
/// A full-screen face that has had no lid reading for this long since it
/// started stops waiting for one: a broken brainstem shows a sick face, not a
/// black panel.
pub const LID_GRACE: Duration = Duration::from_secs(4);
/// After asking the brainstem to show the face, give it this long to answer
/// before showing the face without it.
const RECOVER_WAIT: Duration = Duration::from_secs(2);
/// On the appliance, a brainstem that has never answered in this long since
/// the face started is missing, not late: the waiting look turns to dazed.
pub const LOADING: Duration = Duration::from_secs(60);
/// A first wake older than this is not a birth any more (a face restarted
/// later in the first day must not replay it).
const BIRTH_FRESH: u64 = 180;

/// What the face keeps from the bus beside the animation.
#[derive(Default)]
pub struct Side {
    /// The brainstem authorizes camera capture now.
    pub camera: bool,
    pub captions: Captions,
    /// The brightness, volume or keyboard light just set, for a moment.
    pub level: LevelBar,
    pub look: Option<Prefs>,
    readings: Option<(Readings, Instant)>,
    online: Option<Online>,
    night: Option<(u64, Instant)>,
    pub menu: Menu,
    pub options: Options,
    pub dock: Dock,
    birth: BirthGate,
}

struct Online {
    at: Instant,
    lookup: bool,
    weather: Option<String>,
}

/// The birth ceremony plays once: on a fresh first wake, once per face
/// process, and (on the appliance) once per boot through a marker in /run.
#[derive(Default)]
pub struct BirthGate {
    played: bool,
    marker: Option<PathBuf>,
}

impl Side {
    fn widget_readings(&self, now: Instant) -> Option<Readings> {
        let (values, sampled) = self.readings?;
        now.checked_duration_since(sampled)
            .filter(|age| *age <= READINGS_MAX_AGE)
            .map(|_| values)
    }
}

impl BirthGate {
    pub fn with_marker(marker: Option<PathBuf>) -> BirthGate {
        BirthGate { played: false, marker }
    }

    /// Whether this `life.woke` should start the ceremony.
    fn admit(&mut self, ev: &Event, now_unix: u64) -> bool {
        if self.played || ev.bool("first") != Some(true) {
            return false;
        }
        let fresh = ev.data.get("woke_at").and_then(|v| v.as_u64()).is_some_and(|at| now_unix.saturating_sub(at) <= BIRTH_FRESH);
        if !fresh {
            return false;
        }
        if let Some(marker) = &self.marker {
            if marker.exists() {
                self.played = true;
                return false;
            }
            if let Err(e) = std::fs::write(marker, b"") {
                eprintln!("face: could not note the birth at {}: {e}", marker.display());
            }
        }
        self.played = true;
        true
    }
}

impl Side {
    pub fn new(birth_marker: Option<PathBuf>) -> Side {
        Side { birth: BirthGate::with_marker(birth_marker), ..Side::default() }
    }
}

fn unix_now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs())
}

/// What to do about a lid that has never been reported.
#[derive(Debug, PartialEq, Eq)]
enum LidStep {
    Wait,
    /// Ask the brainstem to light the panel with the lid unknown (it owns the
    /// backlight, and it will not light it for a face it did not ask for).
    AskBrainstem,
    /// The brainstem is gone or did not answer: show the face anyway.
    ShowLocally,
}

/// Waiting for the first lid reading. Once the lid has been reported open or
/// closed, this never acts again: a lid that later goes unknown (a lost
/// switch) keeps the face dark, as before, until F9 recovers it.
struct LidWait {
    started: Instant,
    grace: Option<Duration>,
    known: bool,
    asked: Option<Instant>,
    /// The face is showing on its own, without a lid reading.
    showing: bool,
}

impl LidWait {
    fn new(now: Instant, grace: Option<Duration>) -> LidWait {
        LidWait { started: now, grace, known: false, asked: None, showing: false }
    }

    fn waiting(&self) -> bool {
        self.grace.is_some() && !self.known
    }

    /// A lid reading arrived. Returns whether the face should apply it. An
    /// unknown reading does not blank a face already shown without one.
    fn reading(&mut self, closed: Option<bool>, show_face: bool) -> bool {
        if closed.is_some() {
            self.known = true;
            self.showing = false;
            return true;
        }
        !(self.waiting() && self.showing && !show_face)
    }

    /// A new connection: ask the new brainstem again.
    fn reconnected(&mut self) {
        self.asked = None;
    }

    fn step(&mut self, now: Instant, connected: bool, panel_blank: bool) -> LidStep {
        let Some(grace) = self.grace.filter(|_| !self.known) else {
            return LidStep::Wait;
        };
        if now.duration_since(self.started) < grace {
            return LidStep::Wait;
        }
        if connected && self.asked.is_none() {
            self.asked = Some(now);
            return LidStep::AskBrainstem;
        }
        let unanswered = match self.asked {
            Some(at) => now.duration_since(at) >= RECOVER_WAIT,
            None => true,
        };
        if panel_blank && unanswered && !self.showing {
            self.showing = true;
            return LidStep::ShowLocally;
        }
        LidStep::Wait
    }
}

pub struct Link {
    path: PathBuf,
    client: Option<Client>,
    next_try: Instant,
    last_beat: Instant,
    lid_seq: Option<u64>,
    pending_panel_ready: Option<u64>,
    lid_wait: LidWait,
    started: Instant,
    /// Heard from a brainstem at least once.
    answered: bool,
    /// Show the calm waiting look, not dazed, until a brainstem first
    /// answers (the appliance, where the face starts before everything).
    loading: bool,
    reported_look: Option<Prefs>,
    pub side: Side,
}

impl Link {
    /// `lid_grace`: show the face after this long with no lid reading (the
    /// full-screen appliance face). `None` leaves the lid to the brainstem.
    pub fn new(path: PathBuf, lid_grace: Option<Duration>) -> Link {
        let now = Instant::now();
        Link {
            path,
            client: None,
            next_try: now,
            last_beat: now,
            lid_seq: None,
            pending_panel_ready: None,
            lid_wait: LidWait::new(now, lid_grace),
            started: now,
            answered: false,
            loading: lid_grace.is_some(),
            reported_look: None,
            side: Side::default(),
        }
    }

    /// The brainstem has been silent long enough to look unwell.
    pub fn quiet(&self) -> bool {
        self.last_beat.elapsed() > QUIET
    }

    pub fn widget_readings(&self, now: Instant) -> Option<Readings> {
        if self.client.is_none() || self.quiet() {
            return None;
        }
        self.side.widget_readings(now)
    }

    pub fn menu_action(&mut self, id: &str) {
        self.ui_action(Event::new("ui.action").with("id", id));
    }

    pub fn ui_action(&mut self, action: Event) {
        let mut action = action.with("via", "key");
        if let Some(seq) = self.lid_seq {
            action = action.with("lid_seq", seq);
        }
        self.send(action);
    }

    /// Ask the mind for the next way of talking to Pip (the dock's chip, or
    /// the mode key).
    pub fn chat_mode_next(&mut self) {
        self.menu_action("chat.mode.next");
    }

    /// A line typed into the dock, for the typing bridge to say to Pip. The
    /// brainstem blocks it while the lid is closed, as it blocks `ui.action`.
    pub fn ui_text(&mut self, text: &str) {
        let mut line = Event::new("ui.text").with("text", text);
        if let Some(seq) = self.lid_seq {
            line = line.with("lid_seq", seq);
        }
        self.send(line);
    }

    pub fn night_notice(&self, now: Instant) -> Option<u64> {
        let (stages, arrived) = self.side.night?;
        (self.client.is_some() && !self.quiet() && !self.side.menu.guest()
            && now.duration_since(arrived) < Duration::from_secs(8))
            .then_some(stages)
    }

    pub fn toggle_menu(&mut self) {
        self.side.options.set_open(false);
        let was_open = self.side.menu.open();
        self.side.menu.set_open(!was_open);
        if was_open {
            self.menu_action(MENU_DISMISSED);
        }
    }

    /// Close the Quick Menu and Options from the face side (the Launchpad
    /// key), with the same dismissal receipt as Esc, so the mind does not
    /// keep treating follow-ups as about a menu that is gone.
    pub fn close_chrome(&mut self) {
        self.side.options.set_open(false);
        if self.side.menu.open() {
            self.side.menu.set_open(false);
            self.menu_action(MENU_DISMISSED);
        }
    }

    /// M mutes the mic even while Options or an unsearched Quick Menu holds
    /// the keyboard: neither has a use for the letter, and a privacy key
    /// must not silently do nothing. True when the key was taken.
    pub fn chrome_mute(&mut self, key: Keycode, repeat: bool) -> bool {
        let held = self.side.options.open() || (self.side.menu.open() && !self.side.menu.searching());
        if key != Keycode::M || !held {
            return false;
        }
        if !repeat {
            self.send(Event::new("key.mute"));
        }
        true
    }

    fn online(&self, now: Instant) -> Option<&Online> {
        self.side.online.as_ref().filter(|online| {
            self.client.is_some() && !self.quiet() && !self.side.menu.guest()
                && now.checked_duration_since(online.at).is_some_and(|age| age <= ONLINE_MAX_AGE)
        })
    }

    pub fn lookup_available(&self, now: Instant) -> bool {
        self.online(now).is_some_and(|online| online.lookup)
    }

    pub fn online_weather(&self, now: Instant) -> Option<&str> {
        let online = self.online(now)?;
        Some(online.weather.as_deref().unwrap_or("Weather · Ask for the weather"))
    }

    pub fn menu_key(&mut self, key: Keycode, repeat: bool, options: bool, now: Instant) -> Option<Selection> {
        let online = self.lookup_available(now);
        self.side.menu.set_online(online);
        let was_open = self.side.menu.open();
        let selection = self.side.menu.key(key, repeat, options, now);
        if was_open && !self.side.menu.open() {
            self.menu_action(MENU_DISMISSED);
        }
        selection
    }

    /// A key while the panel is dark and the brainstem is quiet: light the
    /// face here, since nobody else will (task 2.2).
    pub fn unblank_locally(&mut self, anim: &mut Anim) {
        eprintln!("face: a key with the panel dark and the brainstem quiet; showing the face");
        anim.set_lid(None, true, false);
        unblank_backlight();
    }

    /// Seconds since the brainstem was last heard, if it ever was.
    pub fn beat_age(&self) -> Option<f64> {
        (self.answered && self.client.is_some()).then(|| self.last_beat.elapsed().as_secs_f64())
    }

    /// Take in everything waiting on the bus. Never blocks.
    pub fn poll(&mut self, anim: &mut Anim) {
        let now = Instant::now();
        if self.client.is_none() && now >= self.next_try {
            self.next_try = now + RETRY;
            self.client = Client::connect(&self.path, "face", SUBS).ok();
            self.reported_look = None;
            self.side.readings = None;
            self.side.online = None;
            self.side.camera = false;
            self.lid_seq = None;
            self.pending_panel_ready = None;
            if self.client.is_some() {
                self.lid_wait.reconnected();
            }
        }
        let mut closed = false;
        if let Some(client) = &self.client {
            loop {
                match client.try_recv() {
                    Ok(Some(ev)) => {
                        self.last_beat = now;
                        self.answered = true;
                        if ev.topic == "body.lid" {
                            self.side.night = None;
                            self.side.online = None;
                            let lid = ev.bool("closed");
                            if lid != Some(false) {
                                self.side.camera = false;
                            }
                            let seq = ev.data.get("seq").and_then(|seq| seq.as_u64());
                            let snapshot = ev.bool("snapshot").unwrap_or(false);
                            if snapshot || seq.is_none() || seq != self.lid_seq {
                                self.lid_seq = seq;
                                let show_face = ev.bool("show_face").unwrap_or(false);
                                if !self.lid_wait.reading(lid, show_face) {
                                    // Still no lid reading; the face stays up
                                    // on its own and asks this brainstem again.
                                    self.lid_wait.reconnected();
                                    self.pending_panel_ready = None;
                                    continue;
                                }
                                let visible = anim.set_lid(lid, show_face, snapshot);
                                if !snapshot && lid == Some(false) && ev.bool("dreamed") == Some(true) {
                                    anim.dream_completed_on_open();
                                }
                                self.pending_panel_ready = if visible { seq } else { None };
                            }
                        } else if ev.topic == "screen.night" {
                            let seq = ev.data.get("lid_seq").and_then(|seq| seq.as_u64());
                            let stages = ev.data.get("stages").and_then(|stages| stages.as_u64());
                            if seq.is_some() && seq == self.lid_seq && !anim.panel_blank() && !self.side.menu.guest() {
                                self.side.night = stages.filter(|n| (1..=12).contains(n)).map(|n| (n, now));
                            }
                        } else {
                            apply(&ev, anim, &mut self.side);
                        }
                    }
                    Ok(None) => break,
                    Err(_) => {
                        closed = true;
                        break;
                    }
                }
            }
        }
        if closed {
            // A reconnect can land mid-reply with the state still speaking, so
            // quiet the eyes without ending the reply: later levels still count.
            anim.hush_reply();
            self.client = None;
            self.side.camera = false;
            self.side.online = None;
            self.side.night = None;
            self.side.readings = None;
        }
        let lost = now.duration_since(self.last_beat) > QUIET;
        anim.set_link_lost(lost);
        anim.set_loading(self.loading && !self.answered && now.duration_since(self.started) < LOADING);
        if lost {
            self.side.camera = false;
            self.side.captions.link_lost();
        }
        match self.lid_wait.step(now, self.client.is_some(), anim.panel_blank()) {
            LidStep::Wait => {}
            LidStep::AskBrainstem => {
                eprintln!("face: no lid reading yet; asking the brainstem to show the face");
                self.send(Event::new("key.panel_recover"));
            }
            LidStep::ShowLocally => {
                eprintln!(
                    "face: still no lid reading{}; showing the face anyway",
                    if self.client.is_some() { "" } else { " and no brainstem" }
                );
                anim.set_lid(None, true, false);
                // A brainstem that died after powering the panel down (it
                // does that until a face is ready) leaves it dark: power it
                // back up, or the sick face is drawn on a black panel. A live
                // brainstem re-asserts its own choice on its next tick.
                unblank_backlight();
            }
        }
    }

    pub fn send(&mut self, ev: Event) {
        if let Some(client) = &mut self.client {
            if client.send(&ev).is_err() {
                self.client = None;
                self.side.camera = false;
            }
        }
    }

    pub fn report_look(&mut self, look: Prefs) {
        if self.client.is_some() && self.reported_look != Some(look) {
            self.send(look.applied());
            if self.client.is_some() {
                self.reported_look = Some(look);
            }
        }
    }

    /// A new lid sequence still needs a frame before the backlight comes up.
    pub fn pending_panel_wake(&self) -> bool {
        self.pending_panel_ready.is_some()
    }

    /// The first lit frame is in the display buffer, so the brainstem may now
    /// turn on the physical backlight.
    pub fn presented(&mut self) {
        if let Some(seq) = self.pending_panel_ready.take() {
            self.send(Event::new("panel.ready").with("seq", seq));
        }
    }
}

/// Set every backlight's bl_power to FB_BLANK_UNBLANK. Only the panel power
/// attribute: the brightness setting is left alone.
#[cfg(target_os = "linux")]
fn unblank_backlight() {
    let Ok(entries) = std::fs::read_dir("/sys/class/backlight") else { return };
    for entry in entries.flatten() {
        let control = entry.path().join("bl_power");
        match std::fs::read_to_string(&control) {
            Ok(value) if value.trim() != "0" => match std::fs::write(&control, "0") {
                Ok(()) => eprintln!("face: powered the panel back up through {}", control.display()),
                Err(e) => eprintln!("face: could not power the panel up through {}: {e}", control.display()),
            },
            _ => {}
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn unblank_backlight() {}

fn drive(ev: &Event, name: &str) -> Option<f32> {
    ev.f64(name).filter(|v| v.is_finite()).map(|v| v.clamp(0.0, 1.0) as f32)
}

fn apply(ev: &Event, anim: &mut Anim, side: &mut Side) {
    side.captions.event(ev);
    side.level.event(ev, Instant::now());
    match ev.topic.as_str() {
        "screen.prefs" => {
            let look = Prefs::requested(ev);
            if side.look.is_none_or(|old| look.revision >= old.revision) {
                side.look = Some(look);
            }
        }
        "app.privacy" => {
            if let Some(guest) = ev.bool("guest") {
                side.night = None;
                side.online = None;
                side.menu.privacy(guest);
                side.options.set_open(false);
            }
        }
        "screen.online" => {
            side.online = (ev.bool("allowed") == Some(true) && !side.menu.guest()).then(|| Online {
                at: Instant::now(),
                lookup: ev.bool("lookup") == Some(true),
                weather: ev.str("weather").filter(|value| {
                    !value.is_empty() && value.chars().count() <= 72
                        && value.chars().all(|ch| !ch.is_control())
                }).map(str::to_owned),
            });
        }
        "organ.down" if ev.str("name") == Some("mind") => side.online = None,
        "screen.quick_menu" => {
            if let Some(position) = ev.str("position").and_then(Position::parse) {
                side.menu.position = position;
            }
            if let Some(open) = ev.bool("open") {
                if open {
                    side.options.set_open(false);
                }
                side.menu.set_open(open);
            }
            if let Some(open) = ev.bool("options") {
                if open {
                    side.menu.set_open(false);
                }
                side.options.set_open(open && !side.menu.guest());
            }
            if let Some(phrases) = ev.data.get("options_help").and_then(serde_json::Value::as_array) {
                side.options.set_help(phrases.iter().filter_map(serde_json::Value::as_str).take(48).map(str::to_owned).collect());
            }
            if let Some(status) = ev.str("options_status") {
                side.options.set_status(Some(status.to_owned()));
            }
        }
        "app.refused" => {
            if let Some(app) = ev.str("app") {
                side.menu.refused(app);
            }
        }
        "chat.mode" | "chat.notice" => {
            side.dock.event(ev, Instant::now());
            // Written chat shows Pip's words whatever the Captions setting is.
            side.captions.set_typed(side.dock.mode().typed());
        }
        "body.drives" => anim.set_drives(Drives {
            energy: drive(ev, "energy"),
            arousal: drive(ev, "arousal"),
            social: drive(ev, "social"),
            comfort: drive(ev, "comfort"),
        }),
        "camera.policy" => side.camera = ev.bool("allowed") == Some(true),
        "life.woke" => {
            if side.birth.admit(ev, unix_now()) {
                anim.born();
            }
        }
        "state.changed" => {
            if let Some(state) = ev.str("state") {
                let age = if state == "thinking" && matches!(ev.str("prev"), Some("listening" | "attending")) {
                    ev.data.get("speech_end_mono_ms").and_then(|end| end.as_u64())
                        .and_then(|end| bus::monotonic_millis()?.checked_sub(end))
                } else {
                    None
                };
                anim.set_state_at_speech_age(state, ev.bool("muted").unwrap_or(false),
                                             ev.bool("unwell").unwrap_or(false), age);
            }
        }
        "face.express" => {
            if let Some(name) = ev.str("name") {
                anim.express_event(name, ev.f64("hold").unwrap_or(2.5), ev.f64("intensity"), ev.data.get("blend"));
            }
        }
        "body.state" => {
            anim.set_low_battery(ev.bool("battery_low") == Some(true), ev.bool("on_ac"));
            anim.set_breath_effort(drive(ev, "effort"));
            side.readings = Some((Readings::from_body(ev), Instant::now()));
        }
        "stem.budget" => anim.thermal_budget(
            ev.str("pressure"), ev.str("priority"), ev.bool("active") == Some(true), ev.f64("ttl_ms").unwrap_or(0.0)),
        "body.plug" if ev.str("kind") == Some("storage") && ev.bool("attached") == Some(true)
            && !anim.panel_blank() && !anim.asleep() && !anim.muted() =>
        {
            let x = if ev.str("place") == Some("left") { -0.7 } else { 0.0 };
            anim.glance(x, -0.55, 0.5);
        }
        "face.gaze" if ev.bool("present") == Some(false) => anim.clear_gaze(),
        "face.gaze" => {
            let x = ev.f64("x").unwrap_or(0.0) as f32;
            let y = ev.f64("y").unwrap_or(0.0) as f32;
            if let Some(hold) = ev.f64("hold") {
                anim.glance(x, y, hold);
            } else {
                anim.set_gaze(x, y);
            }
        }
        "say.level" => anim.speech_level(ev.f64("v").unwrap_or(0.0) as f32, ev.str("face_cue")),
        "say.stop" => anim.end_reply(),
        "say.done" if ev.bool("final") == Some(true) => anim.end_reply(),
        "hear.missed" => {
            anim.missed();
            if ev.str("reason") == Some("unclear") {
                anim.unclear_hearing();
            }
        }
        "mind.cancel" => anim.end_reply(),
        "mind.reply_done" if ev.bool("spoke") == Some(false) => anim.end_reply(),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_escape_and_toggle_report_menu_dismissal_with_the_lid_sequence() {
        use std::io::{BufRead, BufReader};
        use std::os::unix::net::UnixListener;

        let path = std::env::temp_dir().join(format!("face-menu-{}.sock", std::process::id()));
        let listener = UnixListener::bind(&path).unwrap();
        let mut link = Link::new(path.clone(), None);
        link.client = Some(Client::connect(&path, "face", &[]).unwrap());
        let (stream, _) = listener.accept().unwrap();
        std::fs::remove_file(path).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let mut reader = BufReader::new(stream);
        let mut event = || {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            Event::parse(&line).unwrap()
        };
        assert_eq!(event().topic, "hello");
        link.lid_seq = Some(12);

        // F15 stands in for the Launchpad media key, which SDL never sees.
        for key in [Keycode::Escape, Keycode::F6, Keycode::F15] {
            link.toggle_menu();
            assert!(link.side.menu.open());
            if key == Keycode::Escape {
                link.menu_key(key, false, false, Instant::now());
            } else if key == Keycode::F15 {
                link.close_chrome();
            } else {
                link.toggle_menu();
            }
            assert!(!link.side.menu.open());
            let receipt = event();
            assert_eq!(receipt.topic, "ui.action");
            assert_eq!(receipt.str("id"), Some(MENU_DISMISSED));
            assert_eq!(receipt.str("via"), Some("key"));
            assert_eq!(receipt.data["lid_seq"].as_u64(), Some(12));
        }

        // M still mutes while Options or the unsearched menu is open.
        link.side.options.set_open(true);
        assert!(link.chrome_mute(Keycode::M, false));
        assert_eq!(event().topic, "key.mute");
        assert!(link.chrome_mute(Keycode::M, true), "a held M is taken, not typed");
        assert!(!link.chrome_mute(Keycode::Down, false));
        link.close_chrome();
        assert!(!link.chrome_mute(Keycode::M, false), "with nothing open M takes the ordinary path");
        link.toggle_menu();
        assert!(link.chrome_mute(Keycode::M, false));
        assert_eq!(event().topic, "key.mute");
        link.close_chrome();
        assert_eq!(event().str("id"), Some(MENU_DISMISSED));
    }

    #[test]
    fn widget_readings_expire_independently_of_link_traffic() {
        let mut link = Link::new(PathBuf::new(), None);
        let mut anim = Anim::new(7);
        apply(&Event::new("body.state").with("hour", 9u32).with("minute", 2u32),
              &mut anim, &mut link.side);
        let sampled = link.side.readings.unwrap().1;
        assert!(link.side.widget_readings(sampled + Duration::from_secs(14)).is_some());
        assert!(link.side.widget_readings(sampled + Duration::from_secs(16)).is_none());
        assert!(link.widget_readings(sampled).is_none(), "no reading without a connection");
        link.poll(&mut anim);
        assert!(link.side.readings.is_none(), "a new connection attempt clears the previous generation");
    }

    #[test]
    fn app_refusal_keeps_the_workshop_behind_the_quick_menu() {
        let mut anim = Anim::new(7);
        let mut side = Side::default();
        side.menu.set_open(true);
        let now = Instant::now();
        assert!(side.menu.key(sdl2::keyboard::Keycode::Return, false, false, now).is_some());
        apply(&Event::new("app.refused").with("app", "memory"), &mut anim, &mut side);
        assert!(matches!(side.menu.style_key(false).handoff, crate::quick_menu::Handoff::Waiting { .. }));
        apply(&Event::new("app.refused").with("app", "terminal"), &mut anim, &mut side);
        assert_eq!(side.menu.style_key(false).handoff, crate::quick_menu::Handoff::Refused);
        assert!(side.menu.open());
    }

    #[test]
    fn cancel_drops_the_reply_expression_and_keeps_the_timeout_cue() {
        let mut anim = Anim::new(7);
        anim.set_state("thinking", false, false);
        apply(&Event::new("face.express").with("name", "joy").with("hold", 30.0), &mut anim, &mut Side::default());
        assert_eq!(anim.target_name(), "joy");
        apply(&Event::new("mind.cancel"), &mut anim, &mut Side::default());
        assert_eq!(anim.target_name(), "thinking");
        // The brainstem sends these in this order on a thinking timeout.
        apply(&Event::new("face.express").with("name", "concern"), &mut anim, &mut Side::default());
        apply(&Event::new("state.changed").with("state", "attending"), &mut anim, &mut Side::default());
        assert_eq!(anim.target_name(), "concern");
    }

    #[test]
    fn a_level_just_set_shows_on_the_face() {
        for topic in ["body.brightness", "body.kbd_light", "voice.volume_state"] {
            assert!(SUBS.contains(&topic), "{topic}");
        }
        let mut anim = Anim::new(7);
        let mut side = Side::default();
        apply(&Event::new("voice.volume_state").with("level", 56).with("mixer", "hardware"), &mut anim, &mut side);
        let now = Instant::now();
        assert!(side.level.visible(now));
        assert!(side.level.line(now).is_some_and(|line| line.starts_with("volume") && line.ends_with("56%")));
    }

    #[test]
    fn a_missed_hearing_squints_and_shows_no_caption() {
        assert!(SUBS.contains(&"hear.missed"));
        let mut anim = Anim::new(7);
        let mut side = Side::default();
        anim.set_state("attending", false, false);
        side.captions.toggle();
        side.captions.take_changed();
        apply(&Event::new("hear.missed").with("reason", "no_speech"), &mut anim, &mut side);
        assert_eq!(anim.target_name(), "skepticism");
        apply(&Event::new("hear.missed").with("reason", "unclear"), &mut anim, &mut side);
        assert_eq!(anim.target_name(), "confused");
        assert!(!side.captions.take_changed(), "a miss has no words to caption");
    }

    #[test]
    fn a_silent_reply_discards_its_expression_while_a_spoken_reply_keeps_it() {
        let mut anim = Anim::new(7);
        anim.set_state("thinking", false, false);
        anim.express("joy", 30.0);
        apply(&Event::new("mind.reply_done").with("spoke", false), &mut anim, &mut Side::default());
        assert_eq!(anim.target_name(), "thinking");
        anim.express("joy", 30.0);
        apply(&Event::new("mind.reply_done").with("spoke", true), &mut anim, &mut Side::default());
        apply(&Event::new("state.changed").with("state", "speaking"), &mut anim, &mut Side::default());
        assert_eq!(anim.target_name(), "joy");
    }

    fn at(start: Instant, secs: f64) -> Instant {
        start + Duration::from_secs_f64(secs)
    }

    #[test]
    fn with_no_brainstem_the_face_shows_itself_after_the_grace() {
        let start = Instant::now();
        let mut wait = LidWait::new(start, Some(LID_GRACE));
        assert_eq!(wait.step(at(start, 1.0), false, true), LidStep::Wait);
        assert_eq!(wait.step(at(start, 4.1), false, true), LidStep::ShowLocally);
        assert_eq!(wait.step(at(start, 5.0), false, false), LidStep::Wait);
        // A brainstem that turns up and still has no reading does not blank it.
        wait.reconnected();
        assert!(!wait.reading(None, false));
        assert!(wait.reading(None, true));
    }

    #[test]
    fn a_silent_brainstem_is_asked_first_then_overruled() {
        let start = Instant::now();
        let mut wait = LidWait::new(start, Some(LID_GRACE));
        assert!(wait.reading(None, false));
        assert_eq!(wait.step(at(start, 4.0), true, true), LidStep::AskBrainstem);
        assert_eq!(wait.step(at(start, 5.0), true, true), LidStep::Wait);
        assert_eq!(wait.step(at(start, 6.1), true, true), LidStep::ShowLocally);
    }

    #[test]
    fn a_brainstem_that_answers_keeps_the_handoff() {
        let start = Instant::now();
        let mut wait = LidWait::new(start, Some(LID_GRACE));
        assert_eq!(wait.step(at(start, 4.0), true, true), LidStep::AskBrainstem);
        // The brainstem lights the panel with show_face; the face is no longer blank.
        assert!(wait.reading(None, true));
        assert_eq!(wait.step(at(start, 7.0), true, false), LidStep::Wait);
    }

    #[test]
    fn a_reported_closed_lid_keeps_the_face_dark() {
        let start = Instant::now();
        let mut wait = LidWait::new(start, Some(LID_GRACE));
        assert!(wait.reading(Some(true), false));
        assert_eq!(wait.step(at(start, 60.0), false, true), LidStep::Wait);
        // Even when the switch is lost later on.
        assert!(wait.reading(None, false));
        assert_eq!(wait.step(at(start, 120.0), true, true), LidStep::Wait);
    }

    #[test]
    fn a_face_shown_on_its_own_still_goes_dark_when_the_lid_closes() {
        let start = Instant::now();
        let mut wait = LidWait::new(start, Some(LID_GRACE));
        assert_eq!(wait.step(at(start, 5.0), false, true), LidStep::ShowLocally);
        assert!(wait.reading(Some(true), false));
        assert_eq!(wait.step(at(start, 60.0), true, true), LidStep::Wait);
    }

    #[test]
    fn a_windowed_face_leaves_the_lid_to_the_brainstem() {
        let start = Instant::now();
        let mut wait = LidWait::new(start, None);
        assert_eq!(wait.step(at(start, 60.0), false, true), LidStep::Wait);
        assert!(wait.reading(None, false));
    }

    #[test]
    fn the_birth_plays_once_only_for_a_fresh_first_wake() {
        let now = 1_800_000_000u64;
        let woke = |first: bool, at: u64| Event::new("life.woke").with("first", first).with("woke_at", at);
        let mut gate = BirthGate::default();
        assert!(!gate.admit(&woke(false, now), now), "not the first wake");
        assert!(!gate.admit(&woke(true, now - 600), now), "a first wake ten minutes old is not a birth");
        assert!(gate.admit(&woke(true, now - 5), now));
        assert!(!gate.admit(&woke(true, now - 5), now), "a reconnect does not replay it");

        let marker = std::env::temp_dir().join(format!("face-born-{}", std::process::id()));
        let _ = std::fs::remove_file(&marker);
        assert!(BirthGate::with_marker(Some(marker.clone())).admit(&woke(true, now), now));
        assert!(!BirthGate::with_marker(Some(marker.clone())).admit(&woke(true, now), now), "a restarted face does not replay it");
        let _ = std::fs::remove_file(&marker);
    }

    #[test]
    fn drives_camera_and_birth_reach_the_face() {
        let mut anim = Anim::new(7);
        let mut side = Side::default();
        anim.set_lid(Some(false), false, true);
        anim.set_state("idle", false, false);
        apply(&Event::new("camera.policy").with("allowed", true).with("seq", 3), &mut anim, &mut side);
        assert!(side.camera);
        apply(&Event::new("camera.policy").with("allowed", false).with("seq", 4), &mut anim, &mut side);
        assert!(!side.camera);
        apply(&Event::new("body.drives").with("energy", 0.1).with("arousal", 0.5).with("social", 2.0), &mut anim, &mut side);
        let first = Event::new("life.woke").with("first", true).with("woke_at", unix_now());
        apply(&first, &mut anim, &mut side);
        assert!(anim.being_born());
    }

    #[test]
    fn an_anim_shown_without_a_lid_is_lit() {
        let mut anim = Anim::new(3);
        anim.set_lid(None, false, true);
        assert!(anim.panel_blank());
        assert!(anim.set_lid(None, true, false));
        assert!(!anim.panel_blank());
        anim.set_link_lost(true);
        assert_eq!(anim.target_name(), "dazed");
    }

    #[test]
    fn the_dock_asks_for_modes_and_sends_typed_lines_with_the_lid_sequence() {
        use std::io::{BufRead, BufReader};
        use std::os::unix::net::UnixListener;

        let path = std::env::temp_dir().join(format!("face-chat-{}.sock", std::process::id()));
        let listener = UnixListener::bind(&path).unwrap();
        let mut link = Link::new(path.clone(), None);
        link.client = Some(Client::connect(&path, "face", &[]).unwrap());
        let (stream, _) = listener.accept().unwrap();
        std::fs::remove_file(path).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
        let mut reader = BufReader::new(stream);
        let mut event = || {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            Event::parse(&line).unwrap()
        };
        assert_eq!(event().topic, "hello");
        link.lid_seq = Some(31);

        link.chat_mode_next();
        let ask = event();
        assert_eq!((ask.topic.as_str(), ask.str("id")), ("ui.action", Some("chat.mode.next")));
        assert_eq!(ask.data["lid_seq"].as_u64(), Some(31));

        link.ui_text("set a timer for ten minutes");
        let line = event();
        assert_eq!((line.topic.as_str(), line.str("text")), ("ui.text", Some("set a timer for ten minutes")));
        assert_eq!(line.data["lid_seq"].as_u64(), Some(31));

        // With no lid reading the line carries no sequence (the brainstem decides).
        link.lid_seq = None;
        link.ui_text("hello");
        assert!(event().data.get("lid_seq").is_none());
    }

    #[test]
    fn chat_mode_messages_set_the_dock_and_force_captions_for_written_modes() {
        let mut anim = Anim::new(7);
        let mut side = Side::default();
        let now = Instant::now();
        assert!(!side.captions.displaying(now));
        apply(&Event::new("chat.mode").with("mode", "typed_text").with("input", "typed").with("reply", "text"),
              &mut anim, &mut side);
        assert_eq!(side.dock.mode(), crate::dock::Mode::TypedText);
        apply(&Event::new("say.request").with("text", "Done.").with("reply", "r").with("seq", 0), &mut anim, &mut side);
        apply(&Event::new("say.played").with("reply", "r").with("seq", 0), &mut anim, &mut side);
        assert!(side.captions.displaying(now), "typed replies show with the Captions setting off");
        assert!(!side.captions.on);
        // A guest arriving ends it, whatever mode the mind last said.
        apply(&Event::new("app.privacy").with("guest", true), &mut anim, &mut side);
        assert!(!side.captions.displaying(now));
        apply(&Event::new("app.privacy").with("guest", false), &mut anim, &mut side);
        apply(&Event::new("chat.mode").with("mode", "spoken"), &mut anim, &mut side);
        assert!(!side.captions.displaying(now));
        assert_eq!(side.dock.mode(), crate::dock::Mode::Spoken);
        apply(&Event::new("chat.notice").with("text", "Pip is muted."), &mut anim, &mut side);
        assert_eq!(side.dock.mode(), crate::dock::Mode::Spoken, "a notice does not change the mode");
    }

    #[test]
    fn the_dock_topics_are_subscribed() {
        for topic in ["chat.mode", "chat.notice"] {
            assert!(SUBS.contains(&topic), "{topic}");
        }
        assert_eq!(SUBS.last(), Some(&"chat.notice"), "appended at the end, clear of other lanes' entries");
    }
}

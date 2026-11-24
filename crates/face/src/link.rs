//! The face's connection to the brainstem. It reconnects by itself, and it tells
//! the animation when the brainstem has gone quiet.

use crate::anim::{Anim, Drives};
use crate::keys::LevelBar;
use crate::overlay::Captions;
use bus::{Client, Event};
use std::path::PathBuf;
use std::time::{Duration, Instant};

/// Words heard and said (hear.*, say.request) arrive for the captions
/// overlay; it drops them unless it is open, and the face never logs them.
const SUBS: &[&str] = &[
    "state.changed",
    "face.*",
    "say.level",
    "say.request",
    "stem.beat",
    "body.lid",
    "body.state",
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
    // What the brightness, volume and keyboard-light keys (or the owner's
    // words) really set, for the level bar (numbers only).
    "body.brightness",
    "body.kbd_light",
    "voice.volume_state",
];
const RETRY: Duration = Duration::from_millis(700);
/// With no heartbeat for this long, the face looks unwell.
const QUIET: Duration = Duration::from_secs(3);
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
    birth: BirthGate,
}

/// The birth ceremony plays once: on a fresh first wake, once per face
/// process, and (on the appliance) once per boot through a marker in /run.
#[derive(Default)]
pub struct BirthGate {
    played: bool,
    marker: Option<PathBuf>,
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
            side: Side::default(),
        }
    }

    /// The brainstem has been silent long enough to look unwell.
    pub fn quiet(&self) -> bool {
        self.last_beat.elapsed() > QUIET
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
                            let seq = ev.data.get("seq").and_then(|seq| seq.as_u64());
                            let snapshot = ev.bool("snapshot").unwrap_or(false);
                            if snapshot || seq.is_none() || seq != self.lid_seq {
                                self.lid_seq = seq;
                                let lid = ev.bool("closed");
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
            self.client = None;
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
            }
        }
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
                anim.set_state(state, ev.bool("muted").unwrap_or(false), ev.bool("unwell").unwrap_or(false));
            }
        }
        "face.express" => {
            if let Some(name) = ev.str("name") {
                anim.express(name, ev.f64("hold").unwrap_or(2.5));
            }
        }
        "body.state" => anim.set_low_battery(ev.bool("battery_low") == Some(true)),
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
        "say.level" => anim.set_level(ev.f64("v").unwrap_or(0.0) as f32),
        "hear.missed" => anim.missed(),
        "mind.cancel" => anim.end_reply(),
        "mind.reply_done" if ev.bool("spoke") == Some(false) => anim.end_reply(),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        apply(&Event::new("hear.missed").with("reason", "unclear"), &mut anim, &mut side);
        assert_eq!(anim.target_name(), "skepticism");
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
}

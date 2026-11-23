//! The face's connection to the brainstem. It reconnects by itself, and it tells
//! the animation when the brainstem has gone quiet.

use crate::anim::Anim;
use bus::{Client, Event};
use std::path::PathBuf;
use std::time::{Duration, Instant};

const SUBS: &[&str] =
    &["state.changed", "face.*", "say.level", "stem.beat", "body.lid", "body.state", "mind.cancel", "mind.reply_done"];
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
        }
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
                            apply(&ev, anim);
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
        anim.set_link_lost(now.duration_since(self.last_beat) > QUIET);
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

fn apply(ev: &Event, anim: &mut Anim) {
    match ev.topic.as_str() {
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
        apply(&Event::new("face.express").with("name", "joy").with("hold", 30.0), &mut anim);
        assert_eq!(anim.target_name(), "joy");
        apply(&Event::new("mind.cancel"), &mut anim);
        assert_eq!(anim.target_name(), "thinking");
        // The brainstem sends these in this order on a thinking timeout.
        apply(&Event::new("face.express").with("name", "concern"), &mut anim);
        apply(&Event::new("state.changed").with("state", "attending"), &mut anim);
        assert_eq!(anim.target_name(), "concern");
    }

    #[test]
    fn a_silent_reply_discards_its_expression_while_a_spoken_reply_keeps_it() {
        let mut anim = Anim::new(7);
        anim.set_state("thinking", false, false);
        anim.express("joy", 30.0);
        apply(&Event::new("mind.reply_done").with("spoke", false), &mut anim);
        assert_eq!(anim.target_name(), "thinking");
        anim.express("joy", 30.0);
        apply(&Event::new("mind.reply_done").with("spoke", true), &mut anim);
        apply(&Event::new("state.changed").with("state", "speaking"), &mut anim);
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

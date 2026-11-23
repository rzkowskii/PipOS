//! The being's state machine.
//!
//! Pure logic: time comes in as seconds, events go in, events come out. Nothing
//! here touches a socket or a clock, so every transition is testable.

use bus::Event;
use std::collections::BTreeSet;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Waking,
    Idle,
    Attending,
    Listening,
    Thinking,
    Speaking,
    Drowsy,
    Asleep,
}

impl State {
    pub fn name(self) -> &'static str {
        match self {
            State::Waking => "waking",
            State::Idle => "idle",
            State::Attending => "attending",
            State::Listening => "listening",
            State::Thinking => "thinking",
            State::Speaking => "speaking",
            State::Drowsy => "drowsy",
            State::Asleep => "asleep",
        }
    }
}

/// Timings, all in seconds.
#[derive(Clone, Debug)]
pub struct Config {
    /// Organs that must be connected for the being to be well.
    pub required: Vec<String>,
    /// Waking always takes at least this long, so the eyes have time to open.
    pub min_wake: f64,
    /// How long after boot a missing organ is tolerated.
    pub boot_grace: f64,
    /// How long it keeps attending after the last exchange.
    pub attend: f64,
    /// Give up listening after this much silence from the ears.
    pub listen_timeout: f64,
    /// Give up on a reply that never comes.
    pub think_timeout: f64,
    /// Give up on speech that never finishes.
    pub speak_timeout: f64,
    /// Idle this long in the dark or at night and it gets drowsy.
    pub drowsy_after: f64,
    /// Drowsy this long and it falls asleep.
    pub asleep_after: f64,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            required: vec!["face".into(), "mind".into(), "voice".into()],
            min_wake: 1.5,
            boot_grace: 20.0,
            attend: 20.0,
            listen_timeout: 15.0,
            think_timeout: 120.0,
            speak_timeout: 60.0,
            drowsy_after: 900.0,
            asleep_after: 180.0,
        }
    }
}

pub struct Machine {
    cfg: Config,
    state: State,
    since: f64,
    boot: f64,
    up: BTreeSet<String>,
    seen: BTreeSet<String>,
    mind_ready: bool,
    announcing: bool,
    muted: bool,
    unwell: bool,
    lid_blocked: bool,
    lid_wake: bool,
    dark: bool,
    night: bool,
    /// It fell asleep by itself, so morning may wake it. A requested sleep lasts
    /// until it is called.
    natural_sleep: bool,
    attend_until: f64,
    then_attend: bool,
    last_activity: f64,
    last_stimulus: f64,
}

impl Machine {
    pub fn new(cfg: Config, now: f64) -> Machine {
        Machine {
            cfg,
            state: State::Waking,
            since: now,
            boot: now,
            up: BTreeSet::new(),
            seen: BTreeSet::new(),
            mind_ready: false,
            announcing: false,
            muted: false,
            unwell: false,
            lid_blocked: false,
            lid_wake: false,
            dark: false,
            night: false,
            natural_sleep: false,
            attend_until: now,
            then_attend: false,
            last_activity: now,
            last_stimulus: now,
        }
    }

    pub fn state(&self) -> State {
        self.state
    }

    #[cfg(test)]
    pub fn muted(&self) -> bool {
        self.muted
    }

    #[cfg(test)]
    pub fn unwell(&self) -> bool {
        self.unwell
    }

    /// The current state as a `state.changed` event, for an organ that just joined.
    pub fn snapshot(&self) -> Event {
        self.changed(None)
    }

    fn changed(&self, prev: Option<State>) -> Event {
        let mut ev = Event::new("state.changed")
            .with("state", self.state.name())
            .with("muted", self.muted)
            .with("unwell", self.unwell);
        if let Some(prev) = prev {
            ev = ev.with("prev", prev.name());
        }
        ev
    }

    fn go(&mut self, to: State, now: f64, out: &mut Vec<Event>) {
        if to == self.state {
            return;
        }
        let prev = self.state;
        self.state = to;
        if !matches!(to, State::Thinking | State::Speaking) {
            self.announcing = false;
        }
        if to != State::Waking {
            self.lid_wake = false;
        }
        self.since = now;
        self.last_activity = now;
        out.push(self.changed(Some(prev)));
    }

    fn attend(&mut self, now: f64, out: &mut Vec<Event>) {
        self.attend_until = now + self.cfg.attend;
        self.go(State::Attending, now, out);
    }

    /// Someone called its name.
    fn engage(&mut self, now: f64, out: &mut Vec<Event>) {
        self.last_stimulus = now;
        match self.state {
            State::Idle | State::Drowsy => self.attend(now, out),
            State::Attending => self.attend_until = now + self.cfg.attend,
            State::Asleep if !self.lid_blocked => {
                self.then_attend = true;
                self.go(State::Waking, now, out);
            }
            // A name heard while the eyes are still opening is answered as
            // soon as waking finishes, rather than silently dropped.
            State::Waking if !self.lid_blocked => self.then_attend = true,
            _ => {}
        }
    }

    fn sleep(&mut self, natural: bool, now: f64, out: &mut Vec<Event>) {
        if self.state == State::Asleep {
            return;
        }
        if matches!(self.state, State::Listening | State::Thinking | State::Speaking) {
            out.push(Event::new("say.stop"));
            out.push(Event::new("mind.cancel"));
        }
        self.natural_sleep = natural;
        self.go(State::Asleep, now, out);
    }

    fn set_muted(&mut self, on: bool, now: f64, out: &mut Vec<Event>) {
        if on == self.muted {
            return;
        }
        self.muted = on;
        out.push(Event::new("voice.mute").with("on", on));
        if on && matches!(self.state, State::Attending | State::Listening) {
            self.go(State::Idle, now, out); // carries the new flag
        } else {
            out.push(self.changed(None));
        }
    }

    fn check_health(&mut self, now: f64, out: &mut Vec<Event>) {
        let late = now - self.boot > self.cfg.boot_grace;
        let unwell = self
            .cfg
            .required
            .iter()
            .any(|organ| !self.up.contains(organ) && (late || self.seen.contains(organ)));
        if unwell != self.unwell {
            self.unwell = unwell;
            out.push(self.changed(None));
        }
    }

    /// The lid blocks interaction until it is confirmed open.
    pub fn set_env(&mut self, lid_blocked: bool, dark: bool, night: bool, now: f64) -> Vec<Event> {
        let mut out = Vec::new();
        let was_blocked = self.lid_blocked;
        self.lid_blocked = lid_blocked;
        self.dark = dark;
        self.night = night;
        if lid_blocked && !was_blocked {
            self.sleep(false, now, &mut out);
        }
        if !lid_blocked && was_blocked && self.state == State::Asleep {
            self.lid_wake = true;
            self.then_attend = false;
            self.go(State::Waking, now, &mut out);
        }
        out
    }

    pub fn on_event(&mut self, ev: &Event, now: f64) -> Vec<Event> {
        let mut out = Vec::new();
        match ev.topic.as_str() {
            "organ.up" => {
                if let Some(name) = ev.str("name") {
                    self.up.insert(name.to_string());
                    self.seen.insert(name.to_string());
                }
                self.check_health(now, &mut out);
            }
            "organ.down" => {
                if let Some(name) = ev.str("name") {
                    self.up.remove(name);
                    if name == "mind" {
                        self.mind_ready = false;
                    }
                    if self.announcing && matches!(name, "mind" | "voice") {
                        out.push(Event::new("say.stop"));
                        out.push(Event::new("mind.cancel"));
                        self.attend(now, &mut out);
                    }
                }
                self.check_health(now, &mut out);
            }
            "mind.ready" => self.mind_ready = true,
            "mind.announce"
                if self.mind_ready
                    && self.up.contains("voice")
                    && !self.lid_blocked
                    && (self.state == State::Idle || ev.bool("idle_only") != Some(true))
                    && matches!(self.state, State::Idle | State::Drowsy | State::Attending) =>
            {
                if let Some(announcement) = ev.str("announcement").filter(|value| !value.is_empty()) {
                    self.announcing = true;
                    self.go(State::Thinking, now, &mut out);
                    out.push(Event::new("stem.announce_ready").with("announcement", announcement));
                }
            }
            "hear.wake" if !self.muted => self.engage(now, &mut out),
            "hear.speech_start" => {
                self.last_activity = now;
                if self.state == State::Attending && !self.muted {
                    self.go(State::Listening, now, &mut out);
                }
            }
            "hear.partial" => self.last_activity = now,
            "hear.final" => {
                if matches!(self.state, State::Attending | State::Listening) && !self.muted {
                    let said = ev.str("text").is_some_and(|t| !t.trim().is_empty());
                    if said {
                        self.last_stimulus = now;
                        self.go(State::Thinking, now, &mut out);
                    } else {
                        self.attend(now, &mut out);
                    }
                }
            }
            "hear.cancel" => {
                if self.state == State::Listening {
                    self.attend(now, &mut out);
                }
            }
            "say.started" => {
                self.last_activity = now;
                if matches!(self.state, State::Thinking | State::Speaking) {
                    self.go(State::Speaking, now, &mut out);
                }
            }
            "say.level" => self.last_activity = now,
            "say.done" => {
                if self.state == State::Speaking && ev.bool("final").unwrap_or(false) {
                    self.last_stimulus = now;
                    self.attend(now, &mut out);
                }
            }
            "mind.reply_done" => {
                // A reply with nothing to say never reaches the voice.
                if self.state == State::Thinking && !ev.bool("spoke").unwrap_or(true) {
                    self.attend(now, &mut out);
                }
            }
            "mind.emotion" => {
                if let Some(name) = ev.str("name") {
                    out.push(
                        Event::new("face.express")
                            .with("name", name)
                            .with("hold", ev.f64("hold").unwrap_or(4.0)),
                    );
                }
            }
            "key.interrupt" => {
                if matches!(self.state, State::Thinking | State::Speaking) {
                    out.push(Event::new("say.stop"));
                    out.push(Event::new("mind.cancel"));
                    self.attend(now, &mut out);
                }
            }
            "key.mute" => self.set_muted(!self.muted, now, &mut out),
            "key.touch" if !self.lid_blocked && !matches!(self.state, State::Asleep | State::Waking) => {
                self.last_stimulus = now;
                out.push(Event::new("face.gaze").with("x", 0.0).with("y", -0.75).with("hold", 0.75));
            }
            "being.sleep" => self.sleep(false, now, &mut out),
            "being.wake" => {
                if self.state == State::Asleep && !self.lid_blocked {
                    self.then_attend = false;
                    self.go(State::Waking, now, &mut out);
                }
            }
            _ => {}
        }
        out
    }

    pub fn on_tick(&mut self, now: f64) -> Vec<Event> {
        let mut out = Vec::new();
        self.check_health(now, &mut out);
        match self.state {
            State::Waking => {
                // A being built without a mind (an early image) does not wait for one.
                let needs_mind = self.cfg.required.iter().any(|o| o == "mind");
                let ready = (self.mind_ready || !needs_mind) && self.cfg.required.iter().all(|o| self.up.contains(o));
                if (ready || self.lid_wake) && now - self.since >= self.cfg.min_wake {
                    if self.then_attend {
                        self.then_attend = false;
                        self.attend(now, &mut out);
                    } else {
                        self.last_stimulus = now;
                        self.go(State::Idle, now, &mut out);
                    }
                }
            }
            State::Attending => {
                if now >= self.attend_until {
                    self.go(State::Idle, now, &mut out);
                }
            }
            State::Listening => {
                if now - self.last_activity > self.cfg.listen_timeout {
                    self.attend(now, &mut out);
                }
            }
            State::Thinking => {
                if now - self.since > self.cfg.think_timeout {
                    out.push(Event::new("mind.cancel").with("reason", "think_timeout"));
                    out.push(Event::new("face.express").with("name", "concern").with("hold", 3.0));
                    self.attend(now, &mut out);
                }
            }
            State::Speaking => {
                if now - self.last_activity > self.cfg.speak_timeout {
                    out.push(Event::new("say.stop").with("reason", "speak_timeout"));
                    self.attend(now, &mut out);
                }
            }
            State::Idle => {
                if (self.dark || self.night) && now - self.last_stimulus > self.cfg.drowsy_after {
                    self.go(State::Drowsy, now, &mut out);
                }
            }
            State::Drowsy => {
                if now - self.since > self.cfg.asleep_after {
                    self.sleep(true, now, &mut out);
                }
            }
            State::Asleep => {
                let morning = !self.dark && !self.night && !self.lid_blocked;
                if self.natural_sleep && morning && now - self.since > 60.0 {
                    self.then_attend = false;
                    self.go(State::Waking, now, &mut out);
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(topic: &str) -> Event {
        Event::new(topic)
    }

    fn up(name: &str) -> Event {
        Event::new("organ.up").with("name", name)
    }

    /// A machine that has finished waking and sits idle at t = 10.
    fn awake() -> Machine {
        let mut m = Machine::new(Config::default(), 0.0);
        for organ in ["face", "mind", "voice"] {
            m.on_event(&up(organ), 0.1);
        }
        m.on_event(&ev("mind.ready"), 0.2);
        m.on_tick(10.0);
        assert_eq!(m.state(), State::Idle);
        m
    }

    fn topics(events: &[Event]) -> Vec<&str> {
        events.iter().map(|e| e.topic.as_str()).collect()
    }

    #[test]
    fn stays_waking_until_every_organ_and_the_mind_are_ready() {
        let mut m = Machine::new(Config::default(), 0.0);
        m.on_event(&up("face"), 0.1);
        m.on_event(&up("voice"), 0.1);
        m.on_tick(5.0);
        assert_eq!(m.state(), State::Waking);
        m.on_event(&up("mind"), 5.1);
        m.on_tick(6.0);
        assert_eq!(m.state(), State::Waking, "mind is connected but has not said it is ready");
        m.on_event(&ev("mind.ready"), 6.1);
        let out = m.on_tick(6.2);
        assert_eq!(m.state(), State::Idle);
        assert_eq!(out[0].str("state"), Some("idle"));
        assert_eq!(out[0].str("prev"), Some("waking"));
    }

    #[test]
    fn without_a_mind_in_its_plan_it_wakes_on_its_face_alone() {
        let cfg = Config { required: vec!["face".into()], ..Config::default() };
        let mut m = Machine::new(cfg, 0.0);
        m.on_tick(5.0);
        assert_eq!(m.state(), State::Waking, "no face yet");
        m.on_event(&up("face"), 5.1);
        m.on_tick(5.2);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn waking_takes_at_least_min_wake() {
        let mut m = Machine::new(Config::default(), 0.0);
        for organ in ["face", "mind", "voice"] {
            m.on_event(&up(organ), 0.0);
        }
        m.on_event(&ev("mind.ready"), 0.0);
        m.on_tick(1.0);
        assert_eq!(m.state(), State::Waking);
        m.on_tick(1.6);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn a_name_heard_while_waking_from_the_lid_is_answered_once_awake() {
        let mut m = awake();
        m.set_env(true, false, false, 20.0);
        assert_eq!(m.state(), State::Asleep);
        m.set_env(false, false, false, 30.0);
        assert_eq!(m.state(), State::Waking);
        m.on_event(&ev("hear.wake"), 30.2);
        assert_eq!(m.state(), State::Waking, "the eyes finish opening first");
        m.on_tick(30.2 + Config::default().min_wake);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_name_while_waking_behind_a_closed_lid_is_still_ignored() {
        let mut m = awake();
        m.set_env(true, false, false, 20.0);
        m.on_event(&ev("hear.wake"), 20.5);
        assert_eq!(m.state(), State::Asleep);
    }

    #[test]
    fn a_full_conversation_turn() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        assert_eq!(m.state(), State::Attending);
        m.on_event(&ev("hear.speech_start"), 11.5);
        assert_eq!(m.state(), State::Listening);
        m.on_event(&Event::new("hear.final").with("text", "hello there"), 13.0);
        assert_eq!(m.state(), State::Thinking);
        m.on_event(&ev("say.started"), 15.0);
        assert_eq!(m.state(), State::Speaking);
        // The queue running dry mid-reply does not end the turn.
        m.on_event(&Event::new("say.done").with("final", false), 16.0);
        assert_eq!(m.state(), State::Speaking);
        m.on_event(&Event::new("say.done").with("final", true), 18.0);
        assert_eq!(m.state(), State::Attending);
        // Follow-ups need no name for a while, then it drifts back to idle.
        m.on_tick(30.0);
        assert_eq!(m.state(), State::Attending);
        m.on_tick(38.1);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn announcement_waits_for_a_free_voice_and_can_retry() {
        let announce = Event::new("mind.announce").with("announcement", "due-1");
        let mut m = awake();
        assert!(m.on_event(&ev("mind.announce"), 10.1).is_empty());
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&ev("hear.speech_start"), 11.1);
        assert!(m.on_event(&announce, 11.2).is_empty());
        assert_eq!(m.state(), State::Listening);

        m.on_event(&ev("hear.cancel"), 12.0);
        let out = m.on_event(&announce, 12.1);
        assert_eq!(m.state(), State::Thinking);
        assert_eq!(topics(&out), ["state.changed", "stem.announce_ready"]);
        assert_eq!(out[1].str("announcement"), Some("due-1"));
        assert!(m.on_event(&announce, 12.2).is_empty());

        m.on_event(&ev("say.started"), 13.0);
        let out = m.on_event(&Event::new("organ.down").with("name", "voice"), 14.0);
        assert_eq!(m.state(), State::Attending);
        assert!(topics(&out).starts_with(&["say.stop", "mind.cancel", "state.changed"]));
        assert!(m.on_event(&announce, 14.1).is_empty());
        m.on_event(&up("voice"), 15.0);
        assert_eq!(topics(&m.on_event(&announce, 15.1)), ["state.changed", "stem.announce_ready"]);
        m.on_event(&ev("key.interrupt"), 15.2);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn idle_only_announcement_yields_to_a_wake() {
        let offer = Event::new("mind.announce").with("announcement", "greeting").with("idle_only", true);
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        assert_eq!(m.state(), State::Attending);
        assert!(m.on_event(&offer, 11.1).is_empty());
        assert_eq!(m.state(), State::Attending);

        m.on_tick(32.0);
        assert_eq!(m.state(), State::Idle);
        let out = m.on_event(&offer, 32.1);
        assert_eq!(m.state(), State::Thinking);
        assert_eq!(topics(&out), ["state.changed", "stem.announce_ready"]);
        assert_eq!(out[1].str("announcement"), Some("greeting"));
    }

    #[test]
    fn speech_is_ignored_until_it_is_attending() {
        let mut m = awake();
        m.on_event(&ev("hear.speech_start"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "talking to someone else"), 12.0);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn empty_speech_goes_back_to_attending() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&ev("hear.speech_start"), 11.5);
        m.on_event(&Event::new("hear.final").with("text", "   "), 12.0);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_silent_reply_ends_the_turn() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "hm"), 12.0);
        assert_eq!(m.state(), State::Thinking);
        m.on_event(&Event::new("mind.reply_done").with("spoke", false), 13.0);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn interrupt_stops_speech_and_thought() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "tell me a long story"), 12.0);
        m.on_event(&ev("say.started"), 14.0);
        let out = m.on_event(&ev("key.interrupt"), 15.0);
        assert_eq!(topics(&out), ["say.stop", "mind.cancel", "state.changed"]);
        assert_eq!(m.state(), State::Attending);
        // Outside a reply, the key does nothing.
        assert!(m.on_event(&ev("key.interrupt"), 16.0).is_empty());
    }

    #[test]
    fn a_reply_that_never_comes_times_out() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "are you there"), 12.0);
        assert!(m.on_tick(100.0).is_empty());
        let out = m.on_tick(133.0);
        assert_eq!(topics(&out), ["mind.cancel", "face.express", "state.changed"]);
        assert_eq!(out[0].str("reason"), Some("think_timeout"));
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn stalled_playback_reports_a_speech_timeout() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "hello"), 12.0);
        m.on_event(&ev("say.started"), 13.0);
        let out = m.on_tick(74.0);
        assert_eq!(topics(&out), ["say.stop", "state.changed"]);
        assert_eq!(out[0].str("reason"), Some("speak_timeout"));
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn mute_closes_the_ears() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        let out = m.on_event(&ev("key.mute"), 12.0);
        assert_eq!(topics(&out), ["voice.mute", "state.changed"]);
        assert_eq!(out[1].bool("muted"), Some(true));
        assert_eq!(m.state(), State::Idle);
        assert!(m.muted());
        // Unmuting announces itself without a state change.
        let out = m.on_event(&ev("key.mute"), 13.0);
        assert_eq!(out[0].bool("on"), Some(false));
        assert_eq!(out[1].bool("muted"), Some(false));
        assert!(out[1].str("prev").is_none());
    }

    #[test]
    fn closing_the_lid_sleeps_and_opening_wakes() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "hello"), 12.0);
        let out = m.set_env(true, false, false, 13.0);
        assert_eq!(topics(&out), ["say.stop", "mind.cancel", "state.changed"]);
        assert_eq!(m.state(), State::Asleep);
        // Its name does not wake it while the lid is shut.
        m.on_event(&ev("hear.wake"), 14.0);
        assert_eq!(m.state(), State::Asleep);
        m.set_env(false, false, false, 20.0);
        assert_eq!(m.state(), State::Waking);
        m.on_tick(22.0);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn lid_wake_finishes_without_the_mind_and_preserves_mute() {
        let mut m = awake();
        m.on_event(&ev("key.mute"), 11.0);
        m.on_event(&Event::new("organ.down").with("name", "mind"), 12.0);
        m.set_env(true, false, false, 13.0);
        assert!(m.set_env(true, true, false, 13.1).is_empty());
        m.set_env(false, false, false, 14.0);
        m.on_tick(15.6);
        assert_eq!(m.state(), State::Idle);
        assert!(m.muted() && m.unwell());
        m.on_event(&ev("say.started"), 16.0);
        assert_eq!(m.state(), State::Idle, "late audio must not revive the old turn");
    }

    #[test]
    fn gets_drowsy_in_the_dark_and_wakes_in_the_morning() {
        let mut m = awake();
        m.set_env(false, true, true, 10.0);
        m.on_tick(500.0);
        assert_eq!(m.state(), State::Idle, "not yet");
        m.on_tick(911.0);
        assert_eq!(m.state(), State::Drowsy);
        m.on_tick(1092.0);
        assert_eq!(m.state(), State::Asleep);
        m.on_tick(5000.0);
        assert_eq!(m.state(), State::Asleep, "still dark");
        m.set_env(false, false, false, 30000.0);
        m.on_tick(30000.0);
        assert_eq!(m.state(), State::Waking);
    }

    #[test]
    fn stays_awake_all_day_when_it_is_light() {
        let mut m = awake();
        m.on_tick(50_000.0);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn its_name_wakes_it_and_it_attends() {
        let mut m = awake();
        m.on_event(&ev("being.sleep"), 11.0);
        assert_eq!(m.state(), State::Asleep);
        // A requested sleep is not ended by daylight.
        m.on_tick(5000.0);
        assert_eq!(m.state(), State::Asleep);
        m.on_event(&ev("hear.wake"), 5001.0);
        assert_eq!(m.state(), State::Waking);
        m.on_tick(5003.0);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_lost_organ_makes_it_unwell_until_it_returns() {
        let mut m = awake();
        let out = m.on_event(&Event::new("organ.down").with("name", "voice"), 11.0);
        assert_eq!(out[0].bool("unwell"), Some(true));
        assert!(m.unwell());
        let out = m.on_event(&up("voice"), 12.0);
        assert_eq!(out[0].bool("unwell"), Some(false));
        assert!(!m.unwell());
    }

    #[test]
    fn an_organ_that_never_arrives_shows_after_the_grace_period() {
        let mut m = Machine::new(Config::default(), 0.0);
        m.on_event(&up("face"), 0.1);
        assert!(m.on_tick(10.0).is_empty());
        let out = m.on_tick(21.0);
        assert_eq!(out[0].bool("unwell"), Some(true));
        assert_eq!(m.state(), State::Waking);
    }

    #[test]
    fn emotion_reaches_the_face() {
        let mut m = awake();
        let out = m.on_event(&Event::new("mind.emotion").with("name", "joy"), 11.0);
        assert_eq!(out[0].topic, "face.express");
        assert_eq!(out[0].str("name"), Some("joy"));
    }
}

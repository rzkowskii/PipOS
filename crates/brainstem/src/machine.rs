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
    /// Give up on a reply that has been thinking this long in all, however
    /// steadily the mind reports progress (the hard cap).
    pub think_timeout: f64,
    /// Give up on a reply when a mind that reports progress (`mind.progress`)
    /// has gone this long without a report.
    pub think_stall: f64,
    /// The cap for a mind that has never reported progress: an older mind
    /// cannot say it is still working, so it gets a fixed budget instead.
    pub think_blind: f64,
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
            think_timeout: 600.0,
            think_stall: 45.0,
            think_blind: 300.0,
            speak_timeout: 60.0,
            drowsy_after: 900.0,
            asleep_after: 180.0,
        }
    }
}

/// Where the being stood, coarsely, the last time the brainstem wrote it
/// down. Kept in /run so a restarted brainstem picks up where it was instead
/// of reopening the mic or faking a nap; a reboot clears it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Coarse {
    Waking,
    Awake,
    Asleep,
}

impl Coarse {
    pub fn name(self) -> &'static str {
        match self {
            Coarse::Waking => "waking",
            Coarse::Awake => "awake",
            Coarse::Asleep => "asleep",
        }
    }

    pub fn from_name(name: &str) -> Option<Coarse> {
        match name {
            "waking" => Some(Coarse::Waking),
            "awake" => Some(Coarse::Awake),
            "asleep" => Some(Coarse::Asleep),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Resume {
    pub muted: bool,
    pub coarse: Coarse,
    pub natural_sleep: bool,
    /// It had finished waking at least once before.
    pub woke: bool,
    /// The lid blocked interaction when it was written.
    pub lid_blocked: bool,
}

/// After a restart, an unknown lid is not applied to the state for this
/// long, so the first real reading decides (privacy does not wait: the body
/// and the routing treat an unknown lid as closed from the start).
pub const RESUME_LID_HOLD: f64 = 3.0;

pub struct Machine {
    cfg: Config,
    state: State,
    /// Changes on every state transition, including returning to the same
    /// profile later. Consumers echo it instead of matching names alone.
    state_epoch: u64,
    since: f64,
    boot: f64,
    up: BTreeSet<String>,
    seen: BTreeSet<String>,
    mind_ready: bool,
    /// Exact Mind-owned token while an unsolicited announcement owns the turn.
    announcing: Option<String>,
    muted: bool,
    unwell: bool,
    privacy_fault: bool,
    lid_blocked: bool,
    lid_wake: bool,
    /// The lid was confirmed closed during the current blocked spell.
    closed_seen: bool,
    /// A wake has finished at least once (in this process or, after a
    /// restart, before it). Until then opening the lid waits for the mind.
    woke_once: bool,
    /// An unknown lid is ignored until then (after a restart).
    lid_hold_until: f64,
    /// The mind has said it reports progress while it thinks.
    progress_capable: bool,
    /// The current mind's own `mind.ready`, kept so an organ that joins
    /// later is told it too (it goes out once per mind connection).
    ready_event: Option<Event>,
    /// Some of this reply has been handed to the voice (`say.request`).
    handed: bool,
    /// All of it has: the final clause has been handed to the voice.
    handed_last: bool,
    last_progress: f64,
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
            state_epoch: 0,
            since: now,
            boot: now,
            up: BTreeSet::new(),
            seen: BTreeSet::new(),
            mind_ready: false,
            announcing: None,
            muted: false,
            unwell: false,
            privacy_fault: false,
            lid_blocked: false,
            lid_wake: false,
            closed_seen: false,
            woke_once: false,
            lid_hold_until: f64::NEG_INFINITY,
            progress_capable: false,
            ready_event: None,
            handed: false,
            handed_last: false,
            last_progress: now,
            dark: false,
            night: false,
            natural_sleep: false,
            attend_until: now,
            then_attend: false,
            last_activity: now,
            last_stimulus: now,
        }
    }

    /// Pick up after a brainstem restart within the same boot.
    pub fn resume(cfg: Config, now: f64, saved: &Resume) -> Machine {
        let mut m = Machine::new(cfg, now);
        m.muted = saved.muted;
        m.woke_once = saved.woke;
        m.lid_blocked = saved.lid_blocked;
        m.lid_hold_until = now + RESUME_LID_HOLD;
        match saved.coarse {
            Coarse::Waking => {}
            Coarse::Awake => {
                m.state = State::Idle;
                m.woke_once = true;
            }
            Coarse::Asleep => {
                m.state = State::Asleep;
                m.natural_sleep = saved.natural_sleep;
            }
        }
        m
    }

    /// What a restarted brainstem needs to carry on as itself.
    pub fn persist(&self) -> Resume {
        Resume {
            muted: self.muted,
            coarse: match self.state {
                State::Waking => Coarse::Waking,
                State::Asleep => Coarse::Asleep,
                _ => Coarse::Awake,
            },
            natural_sleep: self.state == State::Asleep && self.natural_sleep,
            woke: self.woke_once,
            lid_blocked: self.lid_blocked,
        }
    }

    pub fn state(&self) -> State {
        self.state
    }

    pub fn state_epoch(&self) -> u64 {
        self.state_epoch
    }

    pub fn muted(&self) -> bool {
        self.muted
    }

    #[cfg(test)]
    pub fn unwell(&self) -> bool {
        self.unwell
    }

    pub fn set_privacy_fault(&mut self, fault: bool, now: f64) -> Vec<Event> {
        self.privacy_fault = fault;
        let mut out = Vec::new();
        self.check_health(now, &mut out);
        out
    }

    /// The current state as a `state.changed` event, for an organ that just joined.
    /// While thinking with the whole reply already with the voice it says so
    /// (`reply_with_voice`): the turn is not orphaned, the voice will finish
    /// it, so a mind that starts into it leaves it alone (turn.py
    /// `_note_inherited_turn`).
    pub fn snapshot(&self) -> Event {
        let ev = self.changed(None);
        if self.state == State::Thinking && self.handed_last {
            ev.with("reply_with_voice", true)
        } else {
            ev
        }
    }

    /// The connected mind's `mind.ready`, for an organ that joined after it
    /// went out (marked `snapshot`), or None while no mind has said it.
    pub fn ready_snapshot(&self) -> Option<Event> {
        self.ready_event.clone().map(|ev| ev.with("snapshot", true))
    }

    fn changed(&self, prev: Option<State>) -> Event {
        let mut ev = Event::new("state.changed")
            .with("state", self.state.name())
            .with("state_epoch", self.state_epoch)
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
        self.state_epoch = self.state_epoch.wrapping_add(1);
        if !matches!(to, State::Thinking | State::Speaking) {
            self.announcing = None;
        }
        if to != State::Waking {
            self.lid_wake = false;
        }
        if prev == State::Waking && matches!(to, State::Idle | State::Attending) {
            self.woke_once = true;
        }
        if to == State::Thinking {
            self.last_progress = now;
            // A new reply: nothing of it is with the voice yet.
            self.handed = false;
            self.handed_last = false;
        }
        self.since = now;
        self.last_activity = now;
        out.push(self.changed(Some(prev)));
    }

    fn attend(&mut self, now: f64, out: &mut Vec<Event>) {
        self.attend_until = now + self.cfg.attend;
        self.go(State::Attending, now, out);
    }

    /// An organ that carries part of a turn went away, or came back as a new
    /// connection (a restart). Either way its part of the turn is gone: a
    /// voice that loses the bus drops its reply and its dictation, and a mind
    /// that loses it cancels its turn (voice.py `connection_lost`, turn.py
    /// `_connection_lost`). Waiting for the speak or think timeout left the
    /// being deaf for 60 s after a voice restart while speaking and 300 s
    /// after a mind restart while thinking (cable log, 25 Nov), because the
    /// voice only listens in a hearing state and a name is not taken while
    /// thinking. So the turn ends now and the ears open. `why` goes out as
    /// the cancel's reason.
    fn organ_left_turn(&mut self, name: &str, why: &str, now: f64, out: &mut Vec<Event>) {
        let replying = matches!(self.state, State::Thinking | State::Speaking);
        let end = match name {
            // An announcement waits on both; the mind retries it later.
            "mind" | "voice" if self.announcing.is_some() && replying => {
                out.push(Event::new("say.stop"));
                true
            }
            // The voice's words are gone: once any of the reply was handed
            // to it, or it was speaking, nothing will finish the turn.
            // Before that, the reply still goes to whichever voice is there.
            "voice" if self.state == State::Speaking || (self.state == State::Thinking && self.handed) => true,
            // It was taking dictation; a new voice is not.
            "voice" if self.state == State::Listening => {
                self.attend(now, out);
                return;
            }
            // The mind's reply is gone unless all of it is already with the
            // voice, which then finishes the turn by itself (say.done final).
            "mind" if replying && !self.handed_last => {
                if self.handed {
                    out.push(Event::new("say.stop"));
                }
                true
            }
            _ => false,
        };
        if end {
            out.push(Event::new("mind.cancel").with("reason", why));
            self.attend(now, out);
        }
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
        let unwell = self.privacy_fault || self
            .cfg
            .required
            .iter()
            .any(|organ| !self.up.contains(organ) && (late || self.seen.contains(organ)));
        if unwell != self.unwell {
            self.unwell = unwell;
            out.push(self.changed(None));
        }
    }

    /// The lid blocks interaction until it is confirmed open. `lid` is the
    /// reading: closed, open, or unknown (`None`).
    pub fn set_env(&mut self, lid: Option<bool>, dark: bool, night: bool, now: f64) -> Vec<Event> {
        let mut out = Vec::new();
        self.dark = dark;
        self.night = night;
        if lid.is_none() && now < self.lid_hold_until {
            return out;
        }
        self.lid_hold_until = f64::NEG_INFINITY;
        let lid_blocked = lid != Some(false);
        let was_blocked = self.lid_blocked;
        self.lid_blocked = lid_blocked;
        if lid_blocked && !was_blocked {
            self.closed_seen = false;
            self.sleep(false, now, &mut out);
        }
        if lid == Some(true) {
            self.closed_seen = true;
        }
        if !lid_blocked && was_blocked && self.state == State::Asleep {
            // Opening a lid that was really closed, once it has been awake,
            // need not wait for the mind. A lid that was only unknown (every
            // boot starts that way) waits for the organs like any wake.
            self.lid_wake = self.closed_seen || self.woke_once;
            self.then_attend = false;
            self.go(State::Waking, now, &mut out);
        }
        out
    }

    /// What the brainstem knew of a mind connection that has ended.
    fn forget_mind(&mut self) {
        self.mind_ready = false;
        self.progress_capable = false;
        self.ready_event = None;
    }

    pub fn on_event(&mut self, ev: &Event, now: f64) -> Vec<Event> {
        let mut out = Vec::new();
        match ev.topic.as_str() {
            "organ.up" => {
                let name = ev.str("name").unwrap_or_default();
                // A voice joining for the first time had no part in a turn
                // (a reply typed during boot); one seen before is a restart.
                // A mind that has just connected never has a turn running.
                let returning = self.seen.contains(name) || name == "mind";
                if !name.is_empty() {
                    self.up.insert(name.to_string());
                    self.seen.insert(name.to_string());
                }
                if name == "mind" {
                    // A new mind connection says it is ready for itself.
                    self.forget_mind();
                }
                self.check_health(now, &mut out);
                if returning {
                    self.organ_left_turn(name, &format!("{name}_restarted"), now, &mut out);
                }
            }
            "organ.down" => {
                if let Some(name) = ev.str("name") {
                    self.up.remove(name);
                    if name == "mind" {
                        self.forget_mind();
                    }
                    self.organ_left_turn(name, &format!("{name}_lost"), now, &mut out);
                }
                self.check_health(now, &mut out);
            }
            "mind.ready" => {
                self.mind_ready = true;
                if ev.bool("progress") == Some(true) {
                    self.progress_capable = true;
                }
                self.ready_event = Some(ev.clone());
            }
            "say.request" => {
                if matches!(self.state, State::Thinking | State::Speaking) {
                    self.handed = true;
                    // The voice treats anything but `last: false` as the end.
                    if ev.bool("last") != Some(false) {
                        self.handed_last = true;
                    }
                }
            }
            "mind.progress" => {
                self.progress_capable = true;
                if self.state == State::Thinking {
                    self.last_progress = now;
                }
            }
            "mind.announce"
                if self.mind_ready
                    && self.up.contains("voice")
                    && !self.lid_blocked
                    && (self.state == State::Idle || ev.bool("idle_only") != Some(true))
                    && matches!(self.state, State::Idle | State::Drowsy | State::Attending) =>
            {
                if let Some(announcement) = ev.str("announcement").filter(|value| !value.is_empty()) {
                    self.announcing = Some(announcement.to_owned());
                    self.go(State::Thinking, now, &mut out);
                    out.push(Event::new("stem.announce_ready").with("announcement", announcement));
                }
            }
            "mind.announce_cancel"
                if ev.str("announcement").is_some_and(|token| {
                    self.announcing.as_deref() == Some(token)
                }) && matches!(self.state, State::Thinking | State::Speaking) =>
            {
                out.push(Event::new("say.stop").with("reason", "announcement_revoked"));
                out.push(Event::new("mind.cancel").with("reason", "announcement_revoked"));
                self.attend(now, &mut out);
            }
            "hear.wake" if !self.muted => self.engage(now, &mut out),
            // Space, Return or a click while the eyes rest: the same as its name.
            "key.attend" if !self.muted => self.engage(now, &mut out),
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
                        if let Some(end) = ev.data.get("speech_end_mono_ms").and_then(|value| value.as_u64()) {
                            let recent = bus::monotonic_millis().and_then(|now| now.checked_sub(end))
                                .is_some_and(|age| age <= 3000);
                            if recent {
                                if let Some(changed) = out.last_mut() {
                                    changed.data.insert("speech_end_mono_ms".into(), end.into());
                                }
                            }
                        }
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
                // A reply with nothing to say never reaches the voice. One
                // whose every clause is already with the voice is not that,
                // whatever the mind says (a new mind giving up a turn it
                // inherited, or a turn that failed after its last clause):
                // the voice speaks it and its say.done final ends the turn.
                if self.state == State::Thinking && !ev.bool("spoke").unwrap_or(true) && !self.handed_last {
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
            "key.mute" => {
                // The M key toggles; "stop listening" (from the mind) says `on: true`.
                let on = ev.bool("on").unwrap_or(!self.muted);
                self.set_muted(on, now, &mut out);
            }
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
                let cap = if self.progress_capable { self.cfg.think_timeout } else { self.cfg.think_blind };
                let stalled = self.progress_capable && now - self.last_progress > self.cfg.think_stall;
                let too_long = now - self.since > cap;
                if stalled || too_long {
                    let why = if too_long { "too_long" } else { "no_progress" };
                    out.push(Event::new("mind.cancel").with("reason", "think_timeout").with("why", why));
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
        m.set_env(Some(true), false, false, 20.0);
        assert_eq!(m.state(), State::Asleep);
        m.set_env(Some(false), false, false, 30.0);
        assert_eq!(m.state(), State::Waking);
        m.on_event(&ev("hear.wake"), 30.2);
        assert_eq!(m.state(), State::Waking, "the eyes finish opening first");
        m.on_tick(30.2 + Config::default().min_wake);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn the_attention_key_is_handled_like_its_name() {
        let mut m = awake();
        m.on_event(&ev("key.attend"), 11.0);
        assert_eq!(m.state(), State::Attending, "resting: the ears open");
        m.on_event(&ev("hear.speech_start"), 11.5);
        assert_eq!(m.state(), State::Listening);
        m.on_event(&ev("key.attend"), 12.0);
        assert_eq!(m.state(), State::Listening, "never interrupts the owner mid-sentence");

        // "Stop listening" twice stays muted; it never toggles back.
        let mut m = awake();
        m.on_event(&Event::new("key.mute").with("on", true), 11.0);
        m.on_event(&Event::new("key.mute").with("on", true), 11.1);
        assert!(m.muted());
        m.on_event(&ev("key.mute"), 11.2);
        assert!(!m.muted(), "the M key toggles");

        // Muted: the key does not open the ears behind the owner's back.
        let mut m = awake();
        m.on_event(&ev("key.mute"), 11.0);
        m.on_event(&ev("key.attend"), 11.5);
        assert_eq!(m.state(), State::Idle);

        // Behind a closed lid it is ignored; asleep with the lid open it wakes and attends.
        let mut m = awake();
        m.set_env(Some(true), false, false, 20.0);
        m.on_event(&ev("key.attend"), 20.5);
        assert_eq!(m.state(), State::Asleep);
        m.set_env(Some(false), false, false, 30.0);
        m.on_tick(30.0 + Config::default().min_wake);
        m.on_event(&ev("being.sleep"), 40.0);
        assert_eq!(m.state(), State::Asleep);
        m.on_event(&ev("key.attend"), 41.0);
        assert_eq!(m.state(), State::Waking);
        m.on_tick(41.0 + Config::default().min_wake);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_name_while_waking_behind_a_closed_lid_is_still_ignored() {
        let mut m = awake();
        m.set_env(Some(true), false, false, 20.0);
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
    fn a_reply_from_a_mind_that_never_reports_progress_gets_a_fixed_budget() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "are you there"), 12.0);
        // A cold turn on this CPU can take minutes: 120 s is no longer the end.
        assert!(m.on_tick(200.0).is_empty());
        let out = m.on_tick(313.0);
        assert_eq!(topics(&out), ["mind.cancel", "face.express", "state.changed"]);
        assert_eq!(out[0].str("reason"), Some("think_timeout"));
        assert_eq!(out[0].str("why"), Some("too_long"));
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn progress_keeps_a_long_reply_alive_until_it_stalls_or_hits_the_cap() {
        let progress = Event::new("mind.progress").with("phase", "prompt").with("done", 100);
        let mut m = awake();
        m.on_event(&Event::new("mind.ready").with("progress", true), 10.5);
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "tell me about your day"), 12.0);
        // Steady progress every 5 s keeps it thinking well past the old 120 s.
        let mut t = 12.0;
        while t < 400.0 {
            t += 5.0;
            m.on_event(&progress, t);
            assert!(m.on_tick(t + 0.1).is_empty(), "cancelled at {t}");
        }
        assert_eq!(m.state(), State::Thinking);
        // Then the mind goes quiet: 45 s later it gives up.
        assert!(m.on_tick(t + 44.0).is_empty());
        let out = m.on_tick(t + 45.5);
        assert_eq!(out[0].str("why"), Some("no_progress"));
        assert_eq!(m.state(), State::Attending);

        // However steady the progress, 600 s in all is the end.
        m.on_event(&Event::new("hear.final").with("text", "and then"), 1000.0);
        let mut t = 1000.0;
        let mut cancelled = None;
        while t < 1700.0 && cancelled.is_none() {
            t += 5.0;
            m.on_event(&progress, t);
            cancelled = m.on_tick(t).into_iter().find(|e| e.topic == "mind.cancel");
        }
        let cancel = cancelled.expect("the hard cap");
        assert_eq!(cancel.str("why"), Some("too_long"));
        assert!((1600.0..1606.0).contains(&t), "{t}");
    }

    #[test]
    fn progress_before_a_turn_does_not_count_toward_it_and_a_new_mind_starts_blind() {
        let mut m = awake();
        m.on_event(&ev("mind.progress"), 10.5);
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "hi"), 100.0);
        assert!(m.on_tick(144.0).is_empty(), "the stall clock starts with the turn");
        assert_eq!(m.on_tick(146.0)[0].str("why"), Some("no_progress"));
        // A restarted mind has not said it reports progress.
        m.on_event(&Event::new("organ.down").with("name", "mind"), 150.0);
        m.on_event(&up("mind"), 151.0);
        m.on_event(&ev("mind.ready"), 151.5);
        m.on_event(&ev("hear.wake"), 152.0);
        m.on_event(&Event::new("hear.final").with("text", "hi again"), 153.0);
        assert!(m.on_tick(300.0).is_empty());
        assert_eq!(m.on_tick(454.0)[0].str("why"), Some("too_long"));
    }

    #[test]
    fn the_physical_boot_waits_for_the_mind_even_though_the_lid_opens_at_once() {
        // On hardware the lid starts unknown, then the switch is read open.
        let mut m = Machine::new(Config::default(), 0.0);
        m.set_env(None, false, false, 0.0);
        assert_eq!(m.state(), State::Asleep);
        m.on_event(&up("face"), 0.3);
        m.set_env(Some(false), false, false, 0.5);
        assert_eq!(m.state(), State::Waking);
        m.on_event(&up("voice"), 1.0);
        m.on_tick(5.0);
        assert_eq!(m.state(), State::Waking, "an unknown lid opening is not a lid wake");
        m.on_event(&up("mind"), 14.0);
        m.on_event(&ev("mind.ready"), 30.0);
        m.on_tick(30.1);
        assert_eq!(m.state(), State::Idle);
        // Once it has woken properly, closing and opening the lid wakes at once.
        m.on_event(&Event::new("organ.down").with("name", "mind"), 40.0);
        m.set_env(Some(true), false, false, 41.0);
        m.set_env(Some(false), false, false, 42.0);
        m.on_tick(43.6);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn booting_with_the_lid_closed_then_opening_it_is_a_lid_wake() {
        let mut m = Machine::new(Config::default(), 0.0);
        m.set_env(None, false, false, 0.0);
        m.set_env(Some(true), false, false, 0.4);
        m.set_env(Some(false), false, false, 9.0);
        m.on_tick(10.6);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn a_restart_resumes_mute_and_state_without_faking_a_nap() {
        let mut m = awake();
        m.on_event(&ev("key.mute"), 11.0);
        let saved = m.persist();
        assert_eq!(saved, Resume { muted: true, coarse: Coarse::Awake, natural_sleep: false, woke: true, lid_blocked: false });

        let mut again = Machine::resume(Config::default(), 0.0, &saved);
        assert_eq!(again.state(), State::Idle, "no waking gesture after a restart");
        assert!(again.muted(), "the mic stays closed");
        assert_eq!(again.snapshot().bool("muted"), Some(true));
        // The lid is unknown for a moment after the restart: no fake sleep.
        assert!(again.set_env(None, false, false, 0.1).is_empty());
        assert!(again.set_env(Some(false), false, false, 0.5).is_empty());
        assert_eq!(again.state(), State::Idle);
        // But a lid that stays unknown is acted on after the hold.
        let mut lost = Machine::resume(Config::default(), 0.0, &saved);
        lost.set_env(None, false, false, RESUME_LID_HOLD + 0.1);
        assert_eq!(lost.state(), State::Asleep);

        // A requested sleep survives a restart, even with the lid open.
        let mut m = awake();
        m.on_event(&ev("being.sleep"), 12.0);
        let saved = m.persist();
        assert_eq!((saved.coarse, saved.natural_sleep), (Coarse::Asleep, false));
        let mut again = Machine::resume(Config::default(), 0.0, &saved);
        again.set_env(None, false, false, 0.1);
        again.set_env(Some(false), false, false, 0.6);
        again.on_tick(5000.0);
        assert_eq!(again.state(), State::Asleep);
        // A natural sleep still wakes in the morning.
        let natural = Resume { natural_sleep: true, ..saved };
        let mut again = Machine::resume(Config::default(), 0.0, &natural);
        again.set_env(Some(false), false, false, 0.2);
        again.on_tick(61.0);
        assert_eq!(again.state(), State::Waking);
        assert_eq!(Coarse::from_name(Coarse::Asleep.name()), Some(Coarse::Asleep));
        assert_eq!(Coarse::from_name("dreaming"), None);
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
        let out = m.set_env(Some(true), false, false, 13.0);
        assert_eq!(topics(&out), ["say.stop", "mind.cancel", "state.changed"]);
        assert_eq!(m.state(), State::Asleep);
        // Its name does not wake it while the lid is shut.
        m.on_event(&ev("hear.wake"), 14.0);
        assert_eq!(m.state(), State::Asleep);
        m.set_env(Some(false), false, false, 20.0);
        assert_eq!(m.state(), State::Waking);
        m.on_tick(22.0);
        assert_eq!(m.state(), State::Idle);
    }

    #[test]
    fn lid_wake_finishes_without_the_mind_and_preserves_mute() {
        let mut m = awake();
        m.on_event(&ev("key.mute"), 11.0);
        m.on_event(&Event::new("organ.down").with("name", "mind"), 12.0);
        m.set_env(Some(true), false, false, 13.0);
        assert!(m.set_env(Some(true), true, false, 13.1).is_empty());
        m.set_env(Some(false), false, false, 14.0);
        m.on_tick(15.6);
        assert_eq!(m.state(), State::Idle);
        assert!(m.muted() && m.unwell());
        m.on_event(&ev("say.started"), 16.0);
        assert_eq!(m.state(), State::Idle, "late audio must not revive the old turn");
    }

    #[test]
    fn gets_drowsy_in_the_dark_and_wakes_in_the_morning() {
        let mut m = awake();
        m.set_env(Some(false), true, true, 10.0);
        m.on_tick(500.0);
        assert_eq!(m.state(), State::Idle, "not yet");
        m.on_tick(911.0);
        assert_eq!(m.state(), State::Drowsy);
        m.on_tick(1092.0);
        assert_eq!(m.state(), State::Asleep);
        m.on_tick(5000.0);
        assert_eq!(m.state(), State::Asleep, "still dark");
        m.set_env(Some(false), false, false, 30000.0);
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

    fn down(name: &str) -> Event {
        Event::new("organ.down").with("name", name)
    }

    fn clause(last: bool) -> Event {
        Event::new("say.request").with("text", if last { "" } else { "Once upon a time." }).with("last", last)
    }

    /// A reply under way: asked at 11-12 s, the first clause handed to the
    /// voice and playing from 14 s, more still to come from the mind.
    fn speaking_a_long_reply() -> Machine {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&ev("hear.speech_start"), 11.2);
        m.on_event(&Event::new("hear.final").with("text", "tell me a long story about the sea"), 12.0);
        m.on_event(&clause(false), 13.9);
        m.on_event(&ev("say.started"), 14.0);
        assert_eq!(m.state(), State::Speaking);
        m
    }

    /// The owner can be heard again: a wake and speech reach listening.
    fn hears_again(m: &mut Machine, t: f64) {
        m.on_event(&ev("hear.wake"), t);
        m.on_event(&ev("hear.speech_start"), t + 0.5);
        assert_eq!(m.state(), State::Listening, "deaf at {t}");
    }

    fn cancel_reason(out: &[Event]) -> Option<&str> {
        out.iter().find(|e| e.topic == "mind.cancel").and_then(|e| e.str("reason"))
    }

    #[test]
    fn before_the_fix_a_restarted_voice_left_it_deaf_until_the_speak_timeout() {
        // What the cable log showed (restart-voice-speaking.json): with no
        // word from the dead voice the brainstem stayed speaking for 60 s,
        // and a voice listens only in a hearing state. The machine still
        // gets there by the timeout when nothing at all is said, as before.
        let mut m = speaking_a_long_reply();
        m.on_event(&ev("hear.wake"), 20.0);
        assert_eq!(m.state(), State::Speaking, "a name while speaking is not taken");
        assert!(m.on_tick(73.9).is_empty());
        assert_eq!(topics(&m.on_tick(74.1)), ["say.stop", "state.changed"]);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_voice_restart_while_speaking_ends_the_turn_at_once() {
        // The new voice connects before the old connection is noticed gone
        // (no organ.down): its hello alone ends the turn.
        let mut m = speaking_a_long_reply();
        let out = m.on_event(&up("voice"), 15.0);
        assert_eq!(topics(&out), ["mind.cancel", "state.changed"], "the mind stops; nothing left to stop in the voice");
        assert_eq!(cancel_reason(&out), Some("voice_restarted"));
        assert_eq!(out[1].str("state"), Some("attending"));
        assert_eq!(out[1].str("prev"), Some("speaking"));
        hears_again(&mut m, 16.0);

        // The old voice died first (organ.down), then the new one said hello.
        let mut m = speaking_a_long_reply();
        let out = m.on_event(&down("voice"), 15.0);
        assert_eq!(cancel_reason(&out), Some("voice_lost"));
        assert_eq!(m.state(), State::Attending);
        let out = m.on_event(&up("voice"), 15.2);
        assert_eq!(cancel_reason(&out), None, "the turn was already over");
        hears_again(&mut m, 16.0);
    }

    #[test]
    fn a_voice_restart_after_the_reply_was_handed_over_ends_it_too() {
        // Every clause went to the old voice, which died before it played one.
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "what time is it"), 12.0);
        m.on_event(&clause(false), 12.5);
        m.on_event(&clause(true), 12.6);
        assert_eq!(m.state(), State::Thinking);
        let out = m.on_event(&up("voice"), 13.0);
        assert_eq!(cancel_reason(&out), Some("voice_restarted"));
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_voice_restart_while_the_mind_is_still_thinking_keeps_the_reply() {
        // Nothing has gone to the voice yet: the new voice speaks all of it.
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "tell me a joke"), 12.0);
        let out = m.on_event(&up("voice"), 13.0);
        assert!(cancel_reason(&out).is_none());
        assert_eq!(m.state(), State::Thinking);
        m.on_event(&clause(false), 14.0);
        m.on_event(&ev("say.started"), 14.1);
        m.on_event(&clause(true), 14.5);
        m.on_event(&Event::new("say.done").with("final", true), 16.0);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn a_voice_restart_while_listening_opens_the_ears_again() {
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&ev("hear.speech_start"), 11.5);
        assert_eq!(m.state(), State::Listening);
        let out = m.on_event(&up("voice"), 12.0);
        assert_eq!(topics(&out), ["state.changed"], "no reply to cancel");
        assert_eq!(m.state(), State::Attending);
        hears_again(&mut m, 12.5);
    }

    #[test]
    fn a_voice_joining_for_the_first_time_does_not_end_a_typed_reply() {
        // A reply typed in the local menu's chat while the voice is still
        // starting: the voice had no part in it.
        let cfg = Config { required: vec!["face".into(), "mind".into()], ..Config::default() };
        let mut m = Machine::new(cfg, 0.0);
        m.on_event(&up("face"), 0.1);
        m.on_event(&up("mind"), 0.1);
        m.on_event(&ev("mind.ready"), 0.2);
        m.on_tick(10.0);
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "hello"), 12.0);
        m.on_event(&clause(false), 12.5);
        assert!(cancel_reason(&m.on_event(&up("voice"), 13.0)).is_none());
        assert_eq!(m.state(), State::Thinking);
    }

    #[test]
    fn before_the_fix_a_restarted_mind_left_it_deaf_until_the_think_budget() {
        // restart-mind-thinking.json: thinking for 300.1 s, 34 wakes unheard.
        // A restarted mind has not reported progress, so it gets the blind cap.
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "tell me a long story about the sea"), 12.0);
        m.on_event(&ev("hear.wake"), 20.0);
        assert_eq!(m.state(), State::Thinking, "a name while thinking is not taken");
        assert!(m.on_tick(311.9).is_empty());
        assert_eq!(m.on_tick(312.1)[0].str("reason"), Some("think_timeout"));
    }

    #[test]
    fn a_mind_restart_while_thinking_ends_the_turn_at_once() {
        for died_first in [false, true] {
            let mut m = awake();
            m.on_event(&Event::new("mind.ready").with("progress", true), 10.5);
            m.on_event(&ev("hear.wake"), 11.0);
            m.on_event(&Event::new("hear.final").with("text", "tell me a long story about the sea"), 12.0);
            m.on_event(&Event::new("mind.progress").with("phase", "prompt"), 12.3);
            let mut out = Vec::new();
            if died_first {
                out = m.on_event(&down("mind"), 12.5);
            }
            out.extend(m.on_event(&up("mind"), 12.6));
            let reason = if died_first { "mind_lost" } else { "mind_restarted" };
            assert_eq!(cancel_reason(&out), Some(reason));
            assert!(!topics(&out).contains(&"say.stop"), "nothing was with the voice");
            assert_eq!(m.state(), State::Attending);
            hears_again(&mut m, 13.0);
            // The new mind starts blind and not yet ready.
            assert!(m.ready_snapshot().is_none());
            m.on_event(&Event::new("hear.final").with("text", "hello again"), 14.0);
            assert!(m.on_tick(14.0 + 100.0).is_empty(), "no stall clock for a mind that never said it reports progress");
        }
    }

    #[test]
    fn a_mind_restart_mid_reply_stops_the_half_said_reply() {
        let mut m = speaking_a_long_reply();
        let out = m.on_event(&up("mind"), 15.0);
        assert_eq!(topics(&out), ["say.stop", "mind.cancel", "state.changed"]);
        assert_eq!(cancel_reason(&out), Some("mind_restarted"));
        assert_eq!(m.state(), State::Attending);
        hears_again(&mut m, 16.0);
    }

    #[test]
    fn a_mind_restart_after_the_whole_reply_reached_the_voice_lets_it_finish() {
        let mut m = speaking_a_long_reply();
        m.on_event(&clause(false), 14.2);
        m.on_event(&clause(true), 14.3);
        let out = m.on_event(&up("mind"), 15.0);
        assert!(out.iter().all(|e| e.topic != "mind.cancel" && e.topic != "say.stop"));
        assert_eq!(m.state(), State::Speaking);
        m.on_event(&Event::new("say.done").with("final", true), 18.0);
        assert_eq!(m.state(), State::Attending);
        // The next reply is tracked afresh.
        m.on_event(&Event::new("hear.final").with("text", "and then"), 19.0);
        m.on_event(&clause(false), 19.5);
        assert_eq!(cancel_reason(&m.on_event(&up("mind"), 20.0)), Some("mind_restarted"));
    }

    #[test]
    fn a_new_minds_empty_reply_does_not_cut_off_a_reply_already_with_the_voice() {
        // The mind handed the whole reply over and died before the voice
        // started it. Its replacement cannot tell that from an orphaned
        // turn, so the snapshot says it, and an empty reply_done from the
        // new mind (an older one, which does not read the mark) is ignored.
        let mut m = awake();
        m.on_event(&ev("hear.wake"), 11.0);
        m.on_event(&Event::new("hear.final").with("text", "what time is it"), 12.0);
        assert!(m.snapshot().data.get("reply_with_voice").is_none());
        m.on_event(&clause(false), 12.2);
        assert!(m.snapshot().data.get("reply_with_voice").is_none(), "only part of it is with the voice");
        m.on_event(&clause(true), 12.3);
        let out = m.on_event(&up("mind"), 12.5);
        assert!(cancel_reason(&out).is_none());
        assert_eq!(m.state(), State::Thinking);
        let snap = m.snapshot();
        assert_eq!((snap.str("state"), snap.bool("reply_with_voice")), (Some("thinking"), Some(true)));
        assert!(m.on_event(&Event::new("mind.reply_done").with("spoke", false), 13.6).is_empty());
        assert_eq!(m.state(), State::Thinking);
        m.on_event(&ev("say.started"), 14.0);
        assert_eq!(m.state(), State::Speaking);
        assert!(m.snapshot().data.get("reply_with_voice").is_none());
        m.on_event(&Event::new("say.done").with("final", true), 16.0);
        assert_eq!(m.state(), State::Attending);
        // The next turn starts unmarked, and an empty reply ends it as before.
        m.on_event(&Event::new("hear.final").with("text", "never mind"), 17.0);
        assert!(m.snapshot().data.get("reply_with_voice").is_none());
        m.on_event(&Event::new("mind.reply_done").with("spoke", false), 17.5);
        assert_eq!(m.state(), State::Attending);
    }

    #[test]
    fn an_announcement_still_ends_when_either_organ_leaves_or_returns() {
        for organ in ["mind", "voice"] {
            for event in [down(organ), up(organ)] {
                let mut m = awake();
                m.on_event(&Event::new("mind.announce").with("announcement", "due-1"), 10.5);
                assert_eq!(m.state(), State::Thinking);
                let out = m.on_event(&event, 11.0);
                assert!(topics(&out).starts_with(&["say.stop", "mind.cancel"]), "{organ} {}", event.topic);
                assert_eq!(m.state(), State::Attending);
            }
        }
    }

    #[test]
    fn the_minds_ready_is_kept_for_organs_that_join_later() {
        let mut m = Machine::new(Config::default(), 0.0);
        assert!(m.ready_snapshot().is_none());
        m.on_event(&up("mind"), 0.1);
        assert!(m.ready_snapshot().is_none(), "connected is not ready");
        m.on_event(&Event::new("mind.ready").with("cortex", true).with("progress", true), 0.2);
        let snap = m.ready_snapshot().expect("kept");
        assert_eq!(snap.topic, "mind.ready");
        assert_eq!(snap.bool("cortex"), Some(true));
        assert_eq!(snap.bool("progress"), Some(true));
        assert_eq!(snap.bool("snapshot"), Some(true));
        // A later ready (the cortex came back) replaces it.
        m.on_event(&Event::new("mind.ready").with("cortex", false), 1.0);
        assert_eq!(m.ready_snapshot().unwrap().bool("cortex"), Some(false));
        // Another organ coming and going changes nothing.
        m.on_event(&up("voice"), 1.1);
        m.on_event(&down("voice"), 1.2);
        assert!(m.ready_snapshot().is_some());
        // The mind leaving, or a new mind connection replacing it, forgets it.
        m.on_event(&down("mind"), 2.0);
        assert!(m.ready_snapshot().is_none());
        m.on_event(&up("mind"), 2.1);
        m.on_event(&ev("mind.ready"), 2.2);
        assert!(m.ready_snapshot().is_some());
        m.on_event(&up("mind"), 3.0);
        assert!(m.ready_snapshot().is_none(), "a replacing connection has not said it yet");
    }

    #[test]
    fn emotion_reaches_the_face() {
        let mut m = awake();
        let out = m.on_event(&Event::new("mind.emotion").with("name", "joy"), 11.0);
        assert_eq!(out[0].topic, "face.express");
        assert_eq!(out[0].str("name"), Some("joy"));
    }
}

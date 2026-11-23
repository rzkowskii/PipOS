//! Looking after an old battery: how long it has left, when to ask to be
//! plugged in, and when to save and power off before the cells cut out.
//!
//! Time to empty comes first and percentage second, because worn cells cut
//! out at a reported 10-30% (IMPROVEMENT-PLAN section 4). Everything here is
//! pure: readings and seconds go in, events and at most one power-off come
//! out. The brainstem runs the power-off; the mind speaks the pleas.

use bus::Event;

/// Thresholds, all calibratable from /etc/being/env (see `from_env`).
#[derive(Clone, Debug, PartialEq)]
pub struct Thresholds {
    /// First plea: at or under this percentage...
    pub low_pct: f64,
    /// ...or under this many minutes left.
    pub low_min: f64,
    /// Second plea: under this many minutes left.
    pub very_low_min: f64,
    /// Save and power off at or under this percentage...
    pub critical_pct: f64,
    /// ...or under this many minutes left...
    pub critical_min: f64,
    /// ...or with the pack voltage under this floor (volts) for `floor_hold`.
    pub voltage_floor: Option<f64>,
    pub floor_hold: f64,
    /// Power off after the lid has been closed on battery this many minutes.
    /// Zero turns it off.
    pub lid_closed_min: f64,
    /// Seconds the mind gets to say goodbye and save before the power goes.
    pub grace: f64,
    /// Smoothing time constant for the power draw, seconds.
    pub smoothing: f64,
    /// Time to empty is trusted only after this many seconds of readings.
    pub settle: f64,
}

impl Default for Thresholds {
    fn default() -> Thresholds {
        Thresholds {
            low_pct: 15.0,
            low_min: 30.0,
            very_low_min: 10.0,
            critical_pct: 5.0,
            critical_min: 5.0,
            // The 13-inch 2012 pack is three cells in series (10.95 V
            // nominal): 9.3 V is about 3.1 V a cell, held for 30 s so a load
            // spike is not mistaken for an empty pack. Calibrate on the Mac.
            voltage_floor: Some(9.3),
            floor_hold: 30.0,
            lid_closed_min: 10.0,
            grace: 20.0,
            smoothing: 60.0,
            settle: 30.0,
        }
    }
}

impl Thresholds {
    /// Defaults, with any `BEING_BATTERY_*` override that parses.
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> (Thresholds, Vec<String>) {
        let mut t = Thresholds::default();
        let mut notes = Vec::new();
        let mut number = |name: &str, slot: &mut f64, range: std::ops::RangeInclusive<f64>| {
            if let Some(text) = get(name) {
                match text.trim().parse::<f64>() {
                    Ok(v) if range.contains(&v) => *slot = v,
                    _ => notes.push(format!("{name}={text:?} ignored (expected {}-{})", range.start(), range.end())),
                }
            }
        };
        number("BEING_BATTERY_LOW_PCT", &mut t.low_pct, 0.0..=100.0);
        number("BEING_BATTERY_LOW_MIN", &mut t.low_min, 0.0..=600.0);
        number("BEING_BATTERY_VERY_LOW_MIN", &mut t.very_low_min, 0.0..=600.0);
        number("BEING_BATTERY_CRITICAL_PCT", &mut t.critical_pct, 0.0..=100.0);
        number("BEING_BATTERY_CRITICAL_MIN", &mut t.critical_min, 0.0..=600.0);
        number("BEING_LID_BATTERY_SHUTDOWN_MIN", &mut t.lid_closed_min, 0.0..=1440.0);
        number("BEING_SHUTDOWN_GRACE", &mut t.grace, 0.0..=120.0);
        if let Some(text) = get("BEING_BATTERY_VOLTAGE_FLOOR") {
            match text.trim() {
                "" | "off" | "0" => t.voltage_floor = None,
                v => match v.parse::<f64>() {
                    Ok(v) if (5.0..=13.0).contains(&v) => t.voltage_floor = Some(v),
                    _ => notes.push(format!("BEING_BATTERY_VOLTAGE_FLOOR={text:?} ignored (volts, 5-13, or off)")),
                },
            }
        }
        (t, notes)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Level {
    Ok,
    Low,
    VeryLow,
    Critical,
}

impl Level {
    pub fn name(self) -> &'static str {
        match self {
            Level::Ok => "ok",
            Level::Low => "low",
            Level::VeryLow => "very_low",
            Level::Critical => "critical",
        }
    }
}

/// One reading of the power supply.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Sample {
    pub percent: Option<f64>,
    pub charging: Option<bool>,
    pub on_ac: Option<bool>,
    /// Energy left, watt-hours.
    pub energy_wh: Option<f64>,
    /// Draw right now, watts.
    pub power_w: Option<f64>,
    pub voltage_v: Option<f64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Reason {
    Critical,
    LidClosedOnBattery,
}

impl Reason {
    pub fn name(self) -> &'static str {
        match self {
            Reason::Critical => "battery_critical",
            Reason::LidClosedOnBattery => "lid_closed_on_battery",
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct Pending {
    reason: Reason,
    deadline: f64,
}

pub struct Battery {
    pub th: Thresholds,
    level: Level,
    /// A plea was made since it was last on AC, so plugging in earns thanks.
    warned: bool,
    ema_w: Option<f64>,
    ema_since: Option<f64>,
    last_sample_at: Option<f64>,
    below_floor_since: Option<f64>,
    closed_on_battery_since: Option<f64>,
    pending: Option<Pending>,
    powered_off: bool,
    minutes_left: Option<f64>,
    /// This body can really power itself off (the hardware body, or a test
    /// that names a command). Elsewhere no shutdown is ever announced, so the
    /// mind is never told to say goodbye to a power-off that will not come.
    pub can_power_off: bool,
    /// After a power-off that failed, no new one is announced before this.
    retry_at: f64,
}

/// After a failed power-off, wait this long before announcing another.
const RETRY_AFTER: f64 = 300.0;

impl Battery {
    pub fn new(th: Thresholds) -> Battery {
        Battery {
            th,
            level: Level::Ok,
            warned: false,
            ema_w: None,
            ema_since: None,
            last_sample_at: None,
            below_floor_since: None,
            closed_on_battery_since: None,
            pending: None,
            powered_off: false,
            minutes_left: None,
            can_power_off: true,
            retry_at: f64::NEG_INFINITY,
        }
    }

    pub fn level(&self) -> Level {
        self.level
    }

    /// Minutes to empty on battery, when the draw has been seen long enough.
    pub fn minutes_left(&self) -> Option<f64> {
        self.minutes_left
    }

    /// A shutdown has been announced and not cancelled.
    pub fn shutting_down(&self) -> Option<&'static str> {
        self.pending.map(|p| p.reason.name())
    }

    /// The power-off command failed: keep watching the battery, and try
    /// again (with a fresh notice and grace) after a while.
    pub fn power_off_failed(&mut self, now: f64) {
        self.powered_off = false;
        self.pending = None;
        self.retry_at = now + RETRY_AFTER;
    }

    fn on_battery(s: &Sample) -> bool {
        s.on_ac == Some(false) && s.charging != Some(true)
    }

    fn smooth(&mut self, s: &Sample, now: f64) {
        let draw = s.power_w.filter(|w| w.is_finite() && *w > 0.0);
        if !Self::on_battery(s) || draw.is_none() {
            if !Self::on_battery(s) {
                self.ema_w = None;
                self.ema_since = None;
            }
            self.last_sample_at = Some(now);
            return;
        }
        let draw = draw.unwrap_or_default();
        let dt = self.last_sample_at.map_or(0.0, |at| (now - at).max(0.0));
        self.ema_w = Some(match self.ema_w {
            None => {
                self.ema_since = Some(now);
                draw
            }
            Some(ema) => {
                let alpha = 1.0 - (-dt / self.th.smoothing.max(1.0)).exp();
                ema + alpha * (draw - ema)
            }
        });
        self.last_sample_at = Some(now);
    }

    fn estimate(&self, s: &Sample, now: f64) -> Option<f64> {
        if !Self::on_battery(s) {
            return None;
        }
        let since = self.ema_since?;
        if now - since < self.th.settle {
            return None;
        }
        let watts = self.ema_w.filter(|w| *w > 0.1)?;
        let wh = s.energy_wh.filter(|e| e.is_finite() && *e >= 0.0)?;
        Some((wh / watts * 60.0).min(24.0 * 60.0))
    }

    fn judge(&mut self, s: &Sample, now: f64) -> Level {
        let pct = s.percent;
        let min = self.minutes_left;
        let th = &self.th;
        let floor = match (th.voltage_floor, s.voltage_v) {
            (Some(floor), Some(v)) if v > 0.0 && v < floor => {
                let since = *self.below_floor_since.get_or_insert(now);
                now - since >= th.floor_hold
            }
            _ => {
                self.below_floor_since = None;
                false
            }
        };
        let th = &self.th;
        if pct.is_some_and(|p| p <= th.critical_pct) || min.is_some_and(|m| m < th.critical_min) || floor {
            Level::Critical
        } else if min.is_some_and(|m| m < th.very_low_min) {
            Level::VeryLow
        } else if pct.is_some_and(|p| p <= th.low_pct) || min.is_some_and(|m| m < th.low_min) {
            Level::Low
        } else {
            Level::Ok
        }
    }

    fn report(&self, prev: Level, s: &Sample) -> Event {
        Event::new("body.battery")
            .with("level", self.level.name())
            .with("prev", prev.name())
            .with("percent", s.percent)
            .with("minutes_left", self.minutes_left.map(|m| m.round()))
            .with("on_ac", s.on_ac)
    }

    /// One pass. `lid_closed` is the confirmed reading (None while unknown).
    /// Returns the events to publish and whether to power off now.
    pub fn update(&mut self, s: &Sample, lid_closed: Option<bool>, mind_saved: bool, now: f64) -> (Vec<Event>, bool) {
        let mut out = Vec::new();
        if self.powered_off {
            return (out, false);
        }
        self.smooth(s, now);
        self.minutes_left = self.estimate(s, now);
        let prev = self.level;

        if s.on_ac == Some(true) {
            // Back on the mains: everything resets, and a warning earns thanks.
            self.below_floor_since = None;
            if prev != Level::Ok || self.warned {
                self.level = Level::Ok;
                out.push(self.report(prev, s).with("plugged", self.warned));
            }
            self.warned = false;
        } else if s.on_ac == Some(false) {
            // On battery the level only climbs: a lighter moment does not
            // take back a plea already made.
            let judged = self.judge(s, now);
            if judged > prev {
                self.level = judged;
                self.warned = true;
                out.push(self.report(prev, s));
            }
        }
        // AC unknown: say nothing new; an unknown is not a reason to act.

        let on_battery = s.on_ac == Some(false);
        if on_battery && lid_closed == Some(true) && self.th.lid_closed_min > 0.0 {
            self.closed_on_battery_since.get_or_insert(now);
        } else {
            self.closed_on_battery_since = None;
        }

        let lid_due = self.closed_on_battery_since.is_some_and(|since| now - since >= self.th.lid_closed_min * 60.0);
        let critical = self.level == Level::Critical && on_battery;
        match self.pending {
            None if (critical || lid_due) && self.can_power_off && now >= self.retry_at => {
                let reason = if critical { Reason::Critical } else { Reason::LidClosedOnBattery };
                self.pending = Some(Pending { reason, deadline: now + self.th.grace });
                out.push(
                    Event::new("stem.shutdown")
                        .with("reason", reason.name())
                        .with("in", self.th.grace)
                        .with("percent", s.percent)
                        .with("minutes_left", self.minutes_left.map(|m| m.round())),
                );
            }
            Some(p) => {
                let still = match p.reason {
                    Reason::Critical => on_battery || s.on_ac.is_none(),
                    Reason::LidClosedOnBattery => lid_due || critical,
                };
                if !still {
                    self.pending = None;
                    out.push(Event::new("stem.shutdown").with("reason", p.reason.name()).with("cancelled", true));
                } else if mind_saved || now >= p.deadline {
                    self.powered_off = true;
                    return (out, true);
                }
            }
            None => {}
        }
        (out, false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn on_battery(percent: f64, energy_wh: f64, power_w: f64) -> Sample {
        Sample {
            percent: Some(percent),
            charging: Some(false),
            on_ac: Some(false),
            energy_wh: Some(energy_wh),
            power_w: Some(power_w),
            voltage_v: Some(11.2),
        }
    }

    fn levels(events: &[Event]) -> Vec<&str> {
        events.iter().filter(|e| e.topic == "body.battery").filter_map(|e| e.str("level")).collect()
    }

    #[test]
    fn time_to_empty_comes_first_and_is_smoothed() {
        let mut b = Battery::new(Thresholds::default());
        // 60% of a worn pack, 10 Wh left, drawing 12 W: 50 minutes.
        let (out, off) = b.update(&on_battery(60.0, 10.0, 12.0), Some(false), false, 0.0);
        assert!(out.is_empty() && !off);
        assert_eq!(b.minutes_left(), None, "not trusted until it settles");
        b.update(&on_battery(60.0, 10.0, 12.0), Some(false), false, 31.0);
        assert!((b.minutes_left().unwrap() - 50.0).abs() < 0.5);
        // A short burst of thinking does not swing the estimate far.
        b.update(&on_battery(60.0, 10.0, 40.0), Some(false), false, 33.0);
        assert!(b.minutes_left().unwrap() > 40.0, "{:?}", b.minutes_left());
        // Draw rises for real and stays: 6 Wh at 16 W is 22 minutes: a plea at 60%.
        let mut t = 33.0;
        let mut said = Vec::new();
        while t < 400.0 {
            t += 2.0;
            let (out, _) = b.update(&on_battery(55.0, 6.0, 16.0), Some(false), false, t);
            said.extend(out);
        }
        assert_eq!(levels(&said), ["low"]);
        assert_eq!(said[0].str("prev"), Some("ok"));
        assert!(said[0].f64("minutes_left").unwrap() < 30.0);
    }

    #[test]
    fn percentage_alone_still_pleads_and_saves() {
        let mut b = Battery::new(Thresholds::default());
        let sample = |p| Sample { percent: Some(p), on_ac: Some(false), charging: Some(false), ..Sample::default() };
        assert!(b.update(&sample(16.0), None, false, 0.0).0.is_empty());
        let (out, _) = b.update(&sample(15.0), None, false, 1.0);
        assert_eq!(levels(&out), ["low"]);
        // Climbing back up a point (a reading wobble) does not undo it.
        assert!(b.update(&sample(16.0), None, false, 2.0).0.is_empty());
        let (out, off) = b.update(&sample(5.0), None, false, 3.0);
        assert_eq!(levels(&out), ["critical"]);
        let shutdown = out.iter().find(|e| e.topic == "stem.shutdown").unwrap();
        assert_eq!(shutdown.str("reason"), Some("battery_critical"));
        assert_eq!(shutdown.f64("in"), Some(20.0));
        assert!(!off, "the mind gets its grace to say goodbye and save");
        assert!(!b.update(&sample(5.0), None, false, 10.0).1);
        assert!(b.update(&sample(4.0), None, false, 23.1).1, "then the power goes");
        assert!(!b.update(&sample(4.0), None, false, 24.0).1, "only once");
    }

    #[test]
    fn the_mind_saying_it_has_saved_ends_the_grace_early() {
        let mut b = Battery::new(Thresholds::default());
        let s = Sample { percent: Some(3.0), on_ac: Some(false), ..Sample::default() };
        b.update(&s, None, false, 0.0);
        assert!(b.update(&s, None, true, 2.0).1);
    }

    #[test]
    fn plugging_in_cancels_a_shutdown_and_says_thanks_after_a_warning() {
        let mut b = Battery::new(Thresholds::default());
        let s = Sample { percent: Some(4.0), on_ac: Some(false), ..Sample::default() };
        b.update(&s, None, false, 0.0);
        assert_eq!(b.shutting_down(), Some("battery_critical"));
        let plugged = Sample { percent: Some(4.0), on_ac: Some(true), charging: Some(true), ..Sample::default() };
        let (out, off) = b.update(&plugged, None, false, 5.0);
        assert!(!off);
        let thanks = out.iter().find(|e| e.topic == "body.battery").unwrap();
        assert_eq!((thanks.str("level"), thanks.str("prev"), thanks.bool("plugged")), (Some("ok"), Some("critical"), Some(true)));
        assert!(out.iter().any(|e| e.topic == "stem.shutdown" && e.bool("cancelled") == Some(true)));
        assert_eq!(b.shutting_down(), None);
        assert!(!b.update(&plugged, None, false, 60.0).1);
        // Plugging in without any warning says nothing.
        let mut calm = Battery::new(Thresholds::default());
        calm.update(&Sample { percent: Some(80.0), on_ac: Some(false), ..Sample::default() }, None, false, 0.0);
        assert!(calm.update(&plugged, None, false, 1.0).0.is_empty());
    }

    #[test]
    fn an_unknown_adapter_is_not_a_reason_to_act() {
        let mut b = Battery::new(Thresholds::default());
        let s = Sample { percent: Some(2.0), on_ac: None, ..Sample::default() };
        for t in 0..100 {
            let (out, off) = b.update(&s, Some(true), false, f64::from(t) * 10.0);
            assert!(out.is_empty() && !off);
        }
    }

    #[test]
    fn a_sagging_voltage_must_hold_before_it_counts() {
        let mut b = Battery::new(Thresholds::default());
        let mut s = Sample { percent: Some(40.0), on_ac: Some(false), voltage_v: Some(9.1), ..Sample::default() };
        assert!(b.update(&s, None, false, 0.0).0.is_empty());
        s.voltage_v = Some(10.8);
        assert!(b.update(&s, None, false, 20.0).0.is_empty(), "a load spike recovered");
        s.voltage_v = Some(9.1);
        assert!(b.update(&s, None, false, 21.0).0.is_empty());
        assert!(b.update(&s, None, false, 50.0).0.is_empty());
        assert_eq!(levels(&b.update(&s, None, false, 51.5).0), ["critical"]);
    }

    #[test]
    fn ten_minutes_closed_on_battery_powers_off_and_opening_cancels() {
        let mut b = Battery::new(Thresholds::default());
        let s = Sample { percent: Some(70.0), on_ac: Some(false), ..Sample::default() };
        b.update(&s, Some(true), false, 0.0);
        assert!(b.update(&s, Some(true), false, 599.0).0.is_empty());
        let (out, off) = b.update(&s, Some(true), false, 600.0);
        assert!(!off);
        assert_eq!(out[0].str("reason"), Some("lid_closed_on_battery"));
        // Opened during the grace: cancelled, and the count starts again.
        let (out, off) = b.update(&s, Some(false), false, 610.0);
        assert!(!off && out[0].bool("cancelled") == Some(true));
        b.update(&s, Some(true), false, 620.0);
        assert!(b.update(&s, Some(true), false, 1210.0).0.is_empty());
        b.update(&s, Some(true), false, 1220.0);
        assert!(b.update(&s, Some(true), false, 1240.5).1);

        // On the mains, a closed lid is just a closed lid.
        let mut b = Battery::new(Thresholds::default());
        let ac = Sample { percent: Some(70.0), on_ac: Some(true), ..Sample::default() };
        for t in 0..100 {
            assert!(!b.update(&ac, Some(true), false, f64::from(t) * 60.0).1);
        }
        // And zero minutes turns the policy off.
        let mut b = Battery::new(Thresholds { lid_closed_min: 0.0, ..Thresholds::default() });
        for t in 0..100 {
            assert!(b.update(&s, Some(true), false, f64::from(t) * 60.0).0.is_empty());
        }
    }

    #[test]
    fn a_body_that_cannot_power_off_never_announces_a_shutdown() {
        let mut b = Battery::new(Thresholds::default());
        b.can_power_off = false;
        let s = Sample { percent: Some(3.0), on_ac: Some(false), ..Sample::default() };
        let mut said = Vec::new();
        for t in 0..1000 {
            let (out, off) = b.update(&s, Some(true), true, f64::from(t));
            assert!(!off);
            said.extend(out);
        }
        assert_eq!(levels(&said), ["critical"], "the plea is still honest");
        assert!(!said.iter().any(|e| e.topic == "stem.shutdown"), "no goodbye for a power-off that will not come");
    }

    #[test]
    fn a_failed_power_off_keeps_watching_and_tries_again_later() {
        let mut b = Battery::new(Thresholds::default());
        let s = Sample { percent: Some(3.0), on_ac: Some(false), ..Sample::default() };
        b.update(&s, None, false, 0.0);
        assert!(b.update(&s, None, false, 21.0).1);
        b.power_off_failed(22.0);
        assert_eq!(b.shutting_down(), None);
        assert!(b.update(&s, None, false, 30.0).0.is_empty(), "not straight away");
        let plugged = Sample { on_ac: Some(true), charging: Some(true), ..s };
        let (out, _) = b.update(&plugged, None, false, 40.0);
        assert_eq!(levels(&out), ["ok"], "still watching after the failure");
        b.update(&s, None, false, 50.0);
        let (out, _) = b.update(&s, None, false, 323.0);
        assert!(out.iter().any(|e| e.topic == "stem.shutdown"), "a fresh notice after the wait");
    }

    #[test]
    fn thresholds_come_from_the_environment_when_they_make_sense() {
        let env = |name: &str| match name {
            "BEING_BATTERY_LOW_PCT" => Some("20".to_string()),
            "BEING_LID_BATTERY_SHUTDOWN_MIN" => Some("ten".to_string()),
            "BEING_BATTERY_VOLTAGE_FLOOR" => Some("off".to_string()),
            "BEING_BATTERY_CRITICAL_PCT" => Some("150".to_string()),
            _ => None,
        };
        let (t, notes) = Thresholds::from_env(env);
        assert_eq!(t.low_pct, 20.0);
        assert_eq!(t.lid_closed_min, 10.0);
        assert_eq!(t.critical_pct, 5.0);
        assert_eq!(t.voltage_floor, None);
        assert_eq!(notes.len(), 2, "{notes:?}");
    }
}

//! What bodycare wants, decided from readings and the being's state. Pure:
//! numbers in, numbers out, so every rule is testable without hardware.

/// Settings, with defaults from IMPROVEMENT-PLAN task 2.8 and section 4.
#[derive(Clone, Debug, PartialEq)]
pub struct Config {
    /// The owner's charge limit (percent). None: never touched. Writing it
    /// is a firmware (SMC) change, so it needs the owner's choice.
    pub charge_limit: Option<i64>,
    /// Re-assert the charge limit this often, seconds (an SMC reset clears it).
    pub charge_every: f64,
    /// On AC, this far over the limit and still charging for `honour_after`
    /// seconds means the cap is not being honoured.
    pub honour_margin: f64,
    pub honour_after: f64,
    /// Package temperature where the fan floor starts to rise, and where it
    /// reaches its top.
    pub fan_start_c: f64,
    pub fan_full_c: f64,
    /// The floor never goes above this share of the fan's maximum.
    pub fan_top_share: f64,
    /// One step of the floor, rpm, at most every `fan_step_every` seconds.
    pub fan_step: i64,
    pub fan_step_every: f64,
    /// Package smoothing time constant, seconds.
    pub smoothing: f64,
    /// Over this, even talking is capped.
    pub hot_package_c: f64,
    pub hot_perf_pct: i64,
    /// Dreams are held to this package temperature by a PI loop on the
    /// performance cap.
    pub dream_target_c: f64,
    pub dream_min_pct: i64,
    /// Battery heat: pause dreams over the first, cap the CPU over the
    /// second, resume both under the third.
    pub battery_pause_c: f64,
    pub battery_cap_c: f64,
    pub battery_resume_c: f64,
    pub battery_hot_perf_pct: i64,
    /// With the battery out, the 60 W adapter must never be exceeded.
    pub no_battery_perf_pct: i64,
    pub no_battery_rapl_w: f64,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            charge_limit: None,
            charge_every: 600.0,
            honour_margin: 5.0,
            honour_after: 2.0 * 3600.0,
            fan_start_c: 60.0,
            fan_full_c: 85.0,
            fan_top_share: 0.85,
            fan_step: 250,
            fan_step_every: 10.0,
            smoothing: 10.0,
            hot_package_c: 92.0,
            hot_perf_pct: 70,
            dream_target_c: 70.0,
            dream_min_pct: 30,
            battery_pause_c: 40.0,
            battery_cap_c: 45.0,
            battery_resume_c: 37.0,
            battery_hot_perf_pct: 50,
            no_battery_perf_pct: 85,
            no_battery_rapl_w: 25.0,
        }
    }
}

/// The being, as the bus last told it. Unknown is a valid answer.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Being {
    pub state: Option<String>,
    pub lid_closed: Option<bool>,
    pub dreaming: bool,
}

impl Being {
    /// The mic is open for speech: keep the fan steady.
    pub fn listening(&self) -> bool {
        self.lid_closed != Some(true) && matches!(self.state.as_deref(), Some("attending" | "listening"))
    }

    pub fn talking(&self) -> bool {
        matches!(self.state.as_deref(), Some("attending" | "listening" | "thinking" | "speaking"))
    }

    pub fn asleep(&self) -> bool {
        self.state.as_deref() == Some("asleep")
    }

    /// Asleep with the lid closed: the only time night work runs.
    pub fn night(&self) -> bool {
        self.asleep() && self.lid_closed == Some(true)
    }
}

/// The CPU caps wanted now, and why.
#[derive(Clone, Debug, PartialEq)]
pub struct CpuCap {
    pub no_turbo: bool,
    pub max_perf_pct: i64,
    pub rapl_w: Option<f64>,
    pub reasons: Vec<&'static str>,
}

impl CpuCap {
    fn uncapped() -> CpuCap {
        CpuCap { no_turbo: false, max_perf_pct: 100, rapl_w: None, reasons: Vec::new() }
    }

    fn cap(&mut self, reason: &'static str, no_turbo: bool, pct: i64, rapl_w: Option<f64>) {
        self.no_turbo |= no_turbo;
        self.max_perf_pct = self.max_perf_pct.min(pct);
        self.rapl_w = match (self.rapl_w, rapl_w) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        };
        self.reasons.push(reason);
    }
}

/// A hysteresis latch for battery heat.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Latch {
    pub on: bool,
}

impl Latch {
    pub fn update(&mut self, value: Option<f64>, over: f64, under: f64) -> bool {
        match value {
            Some(v) if v > over => self.on = true,
            Some(v) if v < under => self.on = false,
            // Unknown or in between: keep what it was.
            _ => {}
        }
        self.on
    }
}

/// The dream PI loop: the performance cap that holds the package at the
/// target. Integral clamped so a long cool spell cannot wind it up.
#[derive(Clone, Debug, PartialEq)]
pub struct DreamLoop {
    integral: f64,
    pub pct: f64,
}

impl Default for DreamLoop {
    fn default() -> DreamLoop {
        DreamLoop { integral: 0.0, pct: 60.0 }
    }
}

impl DreamLoop {
    const KP: f64 = 2.0;
    const KI: f64 = 0.05;
    const BASE: f64 = 60.0;

    pub fn reset(&mut self) {
        *self = DreamLoop::default();
    }

    pub fn update(&mut self, package_c: Option<f64>, target_c: f64, min_pct: i64, dt: f64) -> i64 {
        let Some(temp) = package_c else {
            // Blind: hold the floor, which is always safe.
            self.pct = min_pct as f64;
            return min_pct;
        };
        let error = target_c - temp;
        let limit = (100.0 - Self::BASE) / Self::KI;
        self.integral = (self.integral + error * dt.clamp(0.0, 10.0)).clamp(-limit, limit);
        self.pct = (Self::BASE + Self::KP * error + Self::KI * self.integral).clamp(min_pct as f64, 100.0);
        self.pct.round() as i64
    }
}

/// The CPU caps by state (task 2.8 part 3 and section 4).
pub fn cpu_cap(
    cfg: &Config,
    being: &Being,
    package_c: Option<f64>,
    on_ac: Option<bool>,
    battery_present: Option<bool>,
    battery_hot: bool,
    dream_pct: Option<i64>,
) -> CpuCap {
    let mut cap = CpuCap::uncapped();
    if battery_hot {
        cap.cap("battery_hot", true, cfg.battery_hot_perf_pct, None);
    }
    if package_c.is_some_and(|c| c > cfg.hot_package_c) {
        cap.cap("package_hot", true, cfg.hot_perf_pct, None);
    }
    if battery_present == Some(false) && on_ac == Some(true) {
        cap.cap("no_battery", true, cfg.no_battery_perf_pct, Some(cfg.no_battery_rapl_w));
    }
    if on_ac == Some(false) {
        // Less peak current from a worn pack, whatever the state.
        cap.cap("on_battery", true, 100, None);
    }
    if let Some(pct) = dream_pct {
        // "dreaming" when the mind says so; otherwise the night's work.
        cap.cap(if being.dreaming { "dreaming" } else { "night" }, true, pct, None);
    } else if being.asleep() {
        cap.cap("asleep", true, 100, None);
    }
    if cap.reasons.is_empty() {
        cap.reasons.push(if being.talking() { "talking" } else { "idle" });
    }
    cap
}

/// The fan floor the package temperature asks for, between the factory
/// minimum and a share of the maximum.
pub fn fan_target(cfg: &Config, package_c: Option<f64>, factory_min: i64, fan_max: Option<i64>) -> i64 {
    let top = fan_max.map_or(factory_min, |max| ((max as f64) * cfg.fan_top_share) as i64).max(factory_min);
    let Some(temp) = package_c else { return factory_min };
    let share = ((temp - cfg.fan_start_c) / (cfg.fan_full_c - cfg.fan_start_c)).clamp(0.0, 1.0);
    let raw = factory_min as f64 + share * (top - factory_min) as f64;
    // Whole steps of 50 rpm, so a wobbling temperature does not chase.
    ((raw / 50.0).round() as i64 * 50).clamp(factory_min, top)
}

/// The next floor: at most one step toward the target per step period, held
/// steady while the mic is open, and raised only when the lid is closed or
/// it is not listening.
pub fn fan_next(cfg: &Config, current: i64, target: i64, listening: bool) -> i64 {
    if listening || current == target {
        return current;
    }
    if target > current {
        (current + cfg.fan_step).min(target)
    } else {
        (current - cfg.fan_step).max(target)
    }
}

/// Exponential smoothing of a reading taken `dt` seconds after the last.
pub fn smooth(previous: Option<f64>, reading: Option<f64>, dt: f64, tau: f64) -> Option<f64> {
    match (previous, reading) {
        (Some(p), Some(r)) => Some(p + (1.0 - (-dt.max(0.0) / tau.max(0.1)).exp()) * (r - p)),
        (None, r) => r,
        // A missed reading keeps the last smoothed value one pass.
        (p, None) => p,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn being(state: &str) -> Being {
        Being { state: Some(state.into()), lid_closed: Some(false), dreaming: false }
    }

    #[test]
    fn talking_is_uncapped_unless_the_package_is_too_hot() {
        let cfg = Config::default();
        let cap = cpu_cap(&cfg, &being("thinking"), Some(80.0), Some(true), Some(true), false, None);
        assert_eq!((cap.no_turbo, cap.max_perf_pct, cap.reasons.as_slice()), (false, 100, &["talking"][..]));
        let cap = cpu_cap(&cfg, &being("speaking"), Some(93.0), Some(true), Some(true), false, None);
        assert_eq!((cap.no_turbo, cap.max_perf_pct), (true, 70));
    }

    #[test]
    fn asleep_or_on_battery_means_no_turbo() {
        let cfg = Config::default();
        let cap = cpu_cap(&cfg, &being("asleep"), Some(50.0), Some(true), Some(true), false, None);
        assert_eq!((cap.no_turbo, cap.max_perf_pct, cap.reasons[0]), (true, 100, "asleep"));
        let cap = cpu_cap(&cfg, &being("idle"), Some(50.0), Some(false), Some(true), false, None);
        assert_eq!((cap.no_turbo, cap.reasons[0]), (true, "on_battery"));
        // Unknown AC is not "on battery".
        let cap = cpu_cap(&cfg, &being("idle"), Some(50.0), None, Some(true), false, None);
        assert!(!cap.no_turbo);
    }

    #[test]
    fn without_a_battery_the_adapter_is_protected() {
        let cfg = Config::default();
        let cap = cpu_cap(&cfg, &being("thinking"), Some(50.0), Some(true), Some(false), false, None);
        assert_eq!((cap.no_turbo, cap.max_perf_pct, cap.rapl_w), (true, 85, Some(25.0)));
    }

    #[test]
    fn battery_heat_latches_with_hysteresis() {
        let cfg = Config::default();
        let mut pause = Latch::default();
        let mut capl = Latch::default();
        let step = |p: &mut Latch, c: &mut Latch, t: f64| {
            (p.update(Some(t), cfg.battery_pause_c, cfg.battery_resume_c), c.update(Some(t), cfg.battery_cap_c, cfg.battery_resume_c))
        };
        assert_eq!(step(&mut pause, &mut capl, 39.0), (false, false));
        assert_eq!(step(&mut pause, &mut capl, 41.0), (true, false));
        assert_eq!(step(&mut pause, &mut capl, 46.0), (true, true));
        assert_eq!(step(&mut pause, &mut capl, 40.0), (true, true), "not cool enough yet");
        assert!(pause.update(None, 40.0, 37.0), "unknown keeps the latch");
        assert_eq!(step(&mut pause, &mut capl, 36.5), (false, false));
        let cap = cpu_cap(&cfg, &being("speaking"), Some(50.0), Some(true), Some(true), true, None);
        assert_eq!((cap.no_turbo, cap.max_perf_pct), (true, 50));
    }

    #[test]
    fn the_dream_loop_settles_toward_the_target() {
        let cfg = Config::default();
        let mut pi = DreamLoop::default();
        // A crude thermal model: temperature follows 40 + 0.45 * pct.
        let mut temp = 50.0;
        for _ in 0..600 {
            let pct = pi.update(Some(temp), cfg.dream_target_c, cfg.dream_min_pct, 1.0);
            temp += (40.0 + 0.45 * pct as f64 - temp) * 0.1;
        }
        assert!((temp - cfg.dream_target_c).abs() < 1.5, "{temp}");
        assert!((30..=100).contains(&(pi.pct as i64)));
        // Far too hot: down to the floor at once.
        assert_eq!(pi.update(Some(99.0), 70.0, 30, 1.0), 30);
        assert_eq!(pi.update(None, 70.0, 30, 1.0), 30, "blind is the floor");
        let cap = cpu_cap(&cfg, &Being { state: Some("asleep".into()), lid_closed: Some(true), dreaming: true }, Some(70.0), Some(true), Some(true), false, Some(55));
        assert_eq!((cap.no_turbo, cap.max_perf_pct, cap.reasons[0]), (true, 55, "dreaming"));
    }

    #[test]
    fn the_fan_floor_steps_slowly_and_holds_while_listening() {
        let cfg = Config::default();
        assert_eq!(fan_target(&cfg, Some(50.0), 2000, Some(6200)), 2000);
        assert_eq!(fan_target(&cfg, Some(90.0), 2000, Some(6200)), 5250);
        let mid = fan_target(&cfg, Some(72.5), 2000, Some(6200));
        assert!(mid > 3000 && mid < 4000 && mid % 50 == 0, "{mid}");
        assert_eq!(fan_target(&cfg, None, 2000, Some(6200)), 2000, "blind is the factory floor");
        assert_eq!(fan_target(&cfg, Some(90.0), 2000, None), 2000, "no maximum known, no raise");
        assert_eq!(fan_next(&cfg, 2000, 5250, false), 2250);
        assert_eq!(fan_next(&cfg, 5200, 5250, false), 5250);
        assert_eq!(fan_next(&cfg, 3000, 2000, false), 2750);
        assert_eq!(fan_next(&cfg, 2000, 5250, true), 2000, "held while listening");
        assert_eq!(fan_next(&cfg, 3000, 2000, true), 3000, "held either way");
        let lid_shut = Being { state: Some("attending".into()), lid_closed: Some(true), dreaming: false };
        assert!(!lid_shut.listening());
        assert!(being("listening").listening() && being("attending").listening() && !being("thinking").listening());
    }

    #[test]
    fn smoothing_follows_slowly() {
        assert_eq!(smooth(None, Some(50.0), 1.0, 10.0), Some(50.0));
        let s = smooth(Some(50.0), Some(90.0), 1.0, 10.0).unwrap();
        assert!(s > 53.0 && s < 54.5, "{s}");
        assert_eq!(smooth(Some(50.0), None, 1.0, 10.0), Some(50.0));
    }
}

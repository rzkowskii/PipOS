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
    /// reaches its top. Measured on the MacBookPro9,2 (25 November 2026):
    /// the package sits at 71-82 C near idle, and the SMC's own curve holds
    /// the fan at its 2000 rpm minimum until about 97 C (2105 rpm at 98 C,
    /// 2800-3000 rpm at 100 C). Under sustained model load the package
    /// reaches 95 C after 61 s and 100-101 C after about 110 s, against a
    /// TjMax of 105 C, with no throttling. So the floor starts above
    /// anything idle reaches and is at its top where the SMC starts on its
    /// own: it only ever adds air sooner than the SMC would. (The first
    /// curve, 60-85 C, asked for 3300-4700 rpm at idle in observe mode.)
    pub fan_start_c: f64,
    pub fan_full_c: f64,
    /// The floor never goes above this share of the fan's maximum (rounded
    /// down to whole 50 rpm).
    pub fan_top_share: f64,
    /// The floor rises only under sustained load: the busy share of all CPU
    /// time (not idle, not waiting on I/O), smoothed over `fan_load_tau`
    /// seconds, must climb over `fan_load_on`; the floor falls back once it
    /// drops under `fan_load_off`. Near idle on the laptop (the voice
    /// decoding, the face drawing) it is about 0.15-0.22; the model on two
    /// threads adds 0.5. One reply (a few seconds of model time) never spins
    /// the fan; a long read or a run of turns does, after about 20 s. Over
    /// `hot_package_c` the floor follows the temperature whatever the load,
    /// and with no CPU counters at all it follows the temperature alone.
    pub fan_load_on: f64,
    pub fan_load_off: f64,
    pub fan_load_tau: f64,
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
    /// How often the battery's own files are read, seconds: on AC, on
    /// battery, and on battery at or under `battery_low_pct`. Each read of
    /// the MacBookPro9,2's Smart Battery goes through the embedded
    /// controller (about 126 interrupts and 55-64 ms), so a 1 s poll kept
    /// it busy all the time. An adapter change, or a power-supply uevent
    /// (`hw::POWER_STAMP`, touched by udev), forces a read at once.
    pub battery_every: f64,
    pub battery_unplugged_every: f64,
    pub battery_low_every: f64,
    pub battery_low_pct: f64,
    /// A read forced by a change is read once more this many seconds later:
    /// the gauge's status (Charging, Discharging, Full) can lag the adapter's
    /// `online`, and the driver's 1 s cache can hand back the figures of the
    /// moment of the change.
    pub battery_settle: f64,
    /// Uevents never read the battery more often than this, seconds.
    pub battery_poke_gap: f64,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            charge_limit: None,
            charge_every: 600.0,
            honour_margin: 5.0,
            honour_after: 2.0 * 3600.0,
            fan_start_c: 85.0,
            fan_full_c: 97.0,
            fan_top_share: 0.65,
            fan_load_on: 0.40,
            fan_load_off: 0.25,
            fan_load_tau: 30.0,
            fan_step: 250,
            fan_step_every: 10.0,
            smoothing: 10.0,
            hot_package_c: 92.0,
            hot_perf_pct: 70,
            // The owner's dream target (DECISIONS-OWNER 15).
            dream_target_c: 80.0,
            // The Ivy Bridge laptop has a 38% P-state floor. Care raises
            // this value if a body reports a higher minimum at runtime.
            dream_min_pct: 38,
            battery_pause_c: 40.0,
            battery_cap_c: 45.0,
            battery_resume_c: 37.0,
            battery_hot_perf_pct: 50,
            no_battery_perf_pct: 85,
            no_battery_rapl_w: 25.0,
            battery_every: 60.0,
            battery_unplugged_every: 30.0,
            battery_low_every: 10.0,
            battery_low_pct: 20.0,
            battery_settle: 4.0,
            battery_poke_gap: 2.0,
        }
    }
}

/// The being, as the bus last told it. Unknown is a valid answer.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Being {
    pub state: Option<String>,
    pub lid_closed: Option<bool>,
    pub dreaming: bool,
    /// A state event has arrived on the current brainstem connection.
    pub state_fresh: bool,
    pub state_epoch: Option<u64>,
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

/// A descriptive resource profile. These names deliberately do not imply a
/// kernel write: bodycare publishes the profile as evidence while the owner
/// has not enabled the WP-D8 actuator policy.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResourceProfile {
    DeepSleep,
    IdleAwareness,
    ActiveConversation,
    DeepThought,
    Dreaming,
    Performance,
}

impl ResourceProfile {
    pub fn name(self) -> &'static str {
        match self {
            Self::DeepSleep => "deep_sleep",
            Self::IdleAwareness => "idle_awareness",
            Self::ActiveConversation => "active_conversation",
            Self::DeepThought => "deep_thought",
            Self::Dreaming => "dreaming",
            Self::Performance => "performance",
        }
    }
}

/// The cognitive profile suggested by fresh brainstem state. `None` is the
/// fail-closed answer when that state is stale. Performance is intentionally
/// never selected automatically; it remains an explicit, unimplemented gate.
pub fn resource_profile(being: &Being) -> Option<ResourceProfile> {
    if !being.state_fresh || being.state.is_none() || being.state_epoch.is_none() {
        return None;
    }
    if being.dreaming {
        return Some(ResourceProfile::Dreaming);
    }
    match being.state.as_deref() {
        Some("asleep") if being.lid_closed == Some(true) => Some(ResourceProfile::DeepSleep),
        Some("thinking") => Some(ResourceProfile::DeepThought),
        Some("attending" | "listening" | "speaking") => Some(ResourceProfile::ActiveConversation),
        Some(_) => Some(ResourceProfile::IdleAwareness),
        None => None,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Pressure {
    Nominal,
    Warm,
    Hot,
    Critical,
}

impl Pressure {
    pub fn name(self) -> &'static str {
        match self {
            Self::Nominal => "nominal",
            Self::Warm => "warm",
            Self::Hot => "hot",
            Self::Critical => "critical",
        }
    }
}

/// Observe-only pressure classification. Thresholds reuse bodycare's actual
/// fan and CPU safety gates; "critical" is reserved for the measured 100 C
/// package region or the existing battery CPU-cap threshold.
pub fn pressure(cfg: &Config, package_c: Option<f64>, battery_c: Option<f64>) -> Option<Pressure> {
    if package_c.is_none() && battery_c.is_none() {
        return None;
    }
    let package = match package_c {
        Some(v) if v >= 100.0 => Pressure::Critical,
        Some(v) if v > cfg.hot_package_c => Pressure::Hot,
        Some(v) if v > cfg.fan_start_c => Pressure::Warm,
        _ => Pressure::Nominal,
    };
    let battery = match battery_c {
        Some(v) if v > cfg.battery_cap_c => Pressure::Critical,
        Some(v) if v > cfg.battery_pause_c => Pressure::Hot,
        Some(v) if v > cfg.battery_resume_c => Pressure::Warm,
        _ => Pressure::Nominal,
    };
    Some(package.max(battery))
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

/// Is the CPU loaded enough for the fan floor to follow the temperature?
/// `sustained` is the load latch (`fan_load_on` / `fan_load_off`), None when
/// the CPU's counters cannot be read: then the temperature alone decides.
/// A package over `hot_package_c` counts whatever the load.
pub fn fan_loaded(cfg: &Config, package_c: Option<f64>, sustained: Option<bool>) -> bool {
    sustained.unwrap_or(true) || package_c.is_some_and(|c| c > cfg.hot_package_c)
}

/// The fan floor the package temperature asks for, between the factory
/// minimum and a share of the maximum. Without load (`loaded` false), blind
/// or below `fan_start_c` it is the factory minimum: the SMC's own curve.
pub fn fan_target(cfg: &Config, package_c: Option<f64>, loaded: bool, factory_min: i64, fan_max: Option<i64>) -> i64 {
    let top = fan_max.map_or(factory_min, |max| (max as f64 * cfg.fan_top_share / 50.0).floor() as i64 * 50).max(factory_min);
    let Some(temp) = package_c.filter(|_| loaded) else { return factory_min };
    let span = (cfg.fan_full_c - cfg.fan_start_c).max(1.0);
    let share = ((temp - cfg.fan_start_c) / span).clamp(0.0, 1.0);
    let raw = factory_min as f64 + share * (top - factory_min) as f64;
    // Whole steps of 50 rpm, so a wobbling temperature does not chase.
    ((raw / 50.0).round() as i64 * 50).clamp(factory_min, top)
}

/// The busy share of CPU time between two `/proc/stat` readings of (busy,
/// total) ticks. None for the first reading, or when the counters did not
/// move forward (a reset, or less than a tick).
pub fn busy_share(before: Option<(u64, u64)>, now: Option<(u64, u64)>) -> Option<f64> {
    let ((busy0, total0), (busy1, total1)) = (before?, now?);
    let total = total1.checked_sub(total0).filter(|t| *t > 0)?;
    let busy = busy1.checked_sub(busy0)?;
    Some((busy as f64 / total as f64).clamp(0.0, 1.0))
}

/// The next floor: at most one step toward the target per step period. It is
/// never lowered while the mic is open (the fan stays steady while the owner
/// speaks), but it is always raised: the target is above the factory floor
/// only under load or over `hot_package_c`, and cooling comes before quiet.
pub fn fan_next(cfg: &Config, current: i64, target: i64, listening: bool) -> i64 {
    if current == target || (listening && target <= current) {
        return current;
    }
    if target > current {
        (current + cfg.fan_step).min(target)
    } else {
        (current - cfg.fan_step).max(target)
    }
}

/// The last read of the battery's own files: when, what the adapter and
/// the charge were then, and whether a change caused it (so one follow-up
/// read is due `battery_settle` later).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct BatteryRead {
    pub at: f64,
    pub on_ac: Option<bool>,
    pub percent: Option<f64>,
    pub follow_up: bool,
}

/// Seconds between reads of the battery's files, given the last read.
pub fn battery_every(cfg: &Config, last: &BatteryRead) -> f64 {
    match last.on_ac {
        Some(true) => cfg.battery_every,
        _ if last.percent.is_none_or(|p| p <= cfg.battery_low_pct) => cfg.battery_low_every,
        _ => cfg.battery_unplugged_every,
    }
}

/// Is it time to read the battery's files again? Always the first time,
/// when the adapter has changed since (plugged in or pulled out), when the
/// clock went backwards, after a power-supply uevent (`poked`; at most every
/// `battery_poke_gap`), `battery_settle` after a read a change caused, and
/// once the period for the last state is up.
pub fn battery_due(cfg: &Config, last: Option<&BatteryRead>, on_ac: Option<bool>, poked: bool, now: f64) -> bool {
    let Some(last) = last else { return true };
    if last.on_ac != on_ac || now < last.at {
        return true;
    }
    let since = now - last.at;
    (poked && since >= cfg.battery_poke_gap) || (last.follow_up && since >= cfg.battery_settle) || since >= battery_every(cfg, last)
}

/// Does a read now need a follow-up? Only when a change caused it: the
/// adapter changed since the last read, or a uevent came. The follow-up
/// read itself needs none, so there is exactly one.
pub fn battery_edge(last: Option<&BatteryRead>, on_ac: Option<bool>, poked: bool) -> bool {
    last.is_some_and(|last| last.on_ac != on_ac || poked)
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
        Being { state: Some(state.into()), lid_closed: Some(false), dreaming: false, state_fresh: true, state_epoch: Some(1) }
    }

    #[test]
    fn resource_advice_never_invents_performance_or_survives_unknown_state() {
        assert_eq!(resource_profile(&Being::default()), None);
        assert_eq!(resource_profile(&being("idle")), Some(ResourceProfile::IdleAwareness));
        assert_eq!(resource_profile(&being("listening")), Some(ResourceProfile::ActiveConversation));
        assert_eq!(resource_profile(&being("thinking")), Some(ResourceProfile::DeepThought));
        let asleep = Being { state: Some("asleep".into()), lid_closed: Some(true), dreaming: false, state_fresh: true, state_epoch: Some(1) };
        assert_eq!(resource_profile(&asleep), Some(ResourceProfile::DeepSleep));
        let dream = Being { dreaming: true, ..asleep };
        assert_eq!(resource_profile(&dream), Some(ResourceProfile::Dreaming));
    }

    #[test]
    fn pressure_is_unknown_without_sensors_and_uses_existing_safety_gates() {
        let cfg = Config::default();
        assert_eq!(pressure(&cfg, None, None), None);
        assert_eq!(pressure(&cfg, Some(80.0), Some(35.0)), Some(Pressure::Nominal));
        assert_eq!(pressure(&cfg, Some(86.0), Some(35.0)), Some(Pressure::Warm));
        assert_eq!(pressure(&cfg, Some(93.0), Some(35.0)), Some(Pressure::Hot));
        assert_eq!(pressure(&cfg, Some(80.0), Some(46.0)), Some(Pressure::Critical));
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
        assert!((cfg.dream_min_pct..=100).contains(&(pi.pct as i64)));
        // Far too hot: down to the floor at once.
        assert_eq!(pi.update(Some(99.0), 70.0, cfg.dream_min_pct, 1.0), cfg.dream_min_pct);
        assert_eq!(pi.update(None, 70.0, cfg.dream_min_pct, 1.0), cfg.dream_min_pct, "blind is the floor");
        let cap = cpu_cap(&cfg, &Being { state: Some("asleep".into()), lid_closed: Some(true), dreaming: true, state_fresh: true, state_epoch: Some(1) }, Some(70.0), Some(true), Some(true), false, Some(55));
        assert_eq!((cap.no_turbo, cap.max_perf_pct, cap.reasons[0]), (true, 55, "dreaming"));
    }

    #[test]
    fn the_fan_floor_steps_slowly_and_holds_while_listening() {
        let cfg = Config::default();
        assert_eq!(fan_target(&cfg, Some(50.0), true, 2000, Some(6200)), 2000);
        assert_eq!(fan_target(&cfg, Some(97.0), true, 2000, Some(6200)), 4000, "the top: 65% of 6200, in whole 50s");
        assert_eq!(fan_target(&cfg, Some(104.0), true, 2000, Some(6200)), 4000, "never over the top");
        let mid = fan_target(&cfg, Some(91.0), true, 2000, Some(6200));
        assert_eq!(mid, 3000, "half way from 85 C to 97 C");
        assert_eq!(fan_target(&cfg, None, true, 2000, Some(6200)), 2000, "blind is the factory floor");
        assert_eq!(fan_target(&cfg, Some(90.0), true, 2000, None), 2000, "no maximum known, no raise");
        assert_eq!(fan_target(&cfg, Some(95.0), false, 2000, Some(6200)), 2000, "no sustained load, no raise");
        assert_eq!(fan_next(&cfg, 2000, 4000, false), 2250);
        assert_eq!(fan_next(&cfg, 3900, 4000, false), 4000);
        assert_eq!(fan_next(&cfg, 3000, 2000, false), 2750);
        assert_eq!(fan_next(&cfg, 2000, 4000, true), 2250, "raised even while listening");
        assert_eq!(fan_next(&cfg, 3000, 2000, true), 3000, "never lowered while listening");
        let lid_shut = Being { state: Some("attending".into()), lid_closed: Some(true), dreaming: false, state_fresh: true, state_epoch: Some(1) };
        assert!(!lid_shut.listening());
        assert!(being("listening").listening() && being("attending").listening() && !being("thinking").listening());
    }

    #[test]
    fn near_idle_the_floor_never_leaves_the_factory_minimum() {
        // 25 November 2026, observe mode on the laptop: the first curve
        // (60-85 C) would have set fan1_min to 3300-4700 rpm with the package
        // at its idle 71-82 C. Every idle temperature, at any load, and every
        // temperature short of hot without sustained load, now asks for the
        // factory 2000 rpm, which is what the SMC holds there itself.
        let cfg = Config::default();
        for tenths in 0..=250 {
            let temp = 60.0 + f64::from(tenths) / 10.0; // 60.0-85.0 C
            for loaded in [false, true] {
                assert_eq!(fan_target(&cfg, Some(temp), loaded, 2000, Some(6200)), 2000, "{temp} C, loaded {loaded}");
            }
        }
        for tenths in 0..=70 {
            let temp = 85.0 + f64::from(tenths) / 10.0; // up to 92.0 C
            let loaded = fan_loaded(&cfg, Some(temp), Some(false));
            assert!(!loaded, "{temp} C with an idle CPU is not load");
            assert_eq!(fan_target(&cfg, Some(temp), loaded, 2000, Some(6200)), 2000, "{temp} C");
        }
        // Hot is hot, whatever the load; and with no counters at all the
        // temperature alone decides.
        assert!(fan_loaded(&cfg, Some(92.5), Some(false)));
        assert!(fan_loaded(&cfg, Some(70.0), None));
        assert!(!fan_loaded(&cfg, None, Some(false)));
    }

    #[test]
    fn busy_share_comes_from_counter_deltas() {
        assert_eq!(busy_share(None, Some((10, 100))), None, "the first reading has nothing to compare");
        assert_eq!(busy_share(Some((10, 100)), Some((40, 200))), Some(0.3));
        assert_eq!(busy_share(Some((10, 100)), Some((10, 100))), None, "no time passed");
        assert_eq!(busy_share(Some((50, 200)), Some((10, 100))), None, "counters that went back");
        assert_eq!(busy_share(Some((10, 100)), Some((5, 200))), None, "busy that went back");
        assert_eq!(busy_share(Some((0, 0)), Some((500, 100))), Some(1.0), "never over 1");
    }

    #[test]
    fn the_load_latch_needs_sustained_work_not_one_reply() {
        let cfg = Config::default();
        // Near idle 0.18; the model on two of four threads adds 0.5.
        let run = |seconds: u32, busy: f64, start: f64, latch: &mut Latch| {
            let mut ema = Some(start);
            for _ in 0..seconds {
                ema = smooth(ema, Some(busy), 1.0, cfg.fan_load_tau);
                latch.update(ema, cfg.fan_load_on, cfg.fan_load_off);
            }
            ema.unwrap()
        };
        let mut latch = Latch::default();
        let after_reply = run(5, 0.68, 0.18, &mut latch);
        assert!(!latch.on, "a five-second reply is not sustained load ({after_reply:.2})");
        let mut latch = Latch::default();
        run(15, 0.68, 0.18, &mut latch);
        assert!(!latch.on, "nor is fifteen seconds");
        let ema = run(30, 0.68, 0.18, &mut latch);
        assert!(latch.on, "thirty seconds of the model is ({ema:.2})");
        // Back to idle: released once the smoothed load is under 0.25.
        run(20, 0.18, ema, &mut latch);
        assert!(latch.on, "not at once");
        run(120, 0.18, ema, &mut latch);
        assert!(!latch.on);
    }

    #[test]
    fn the_battery_is_read_slowly_and_at_once_when_the_adapter_changes() {
        let cfg = Config::default();
        assert!(battery_due(&cfg, None, Some(true), false, 0.0), "the first pass reads it");
        let on_ac = BatteryRead { at: 100.0, on_ac: Some(true), percent: Some(99.0), follow_up: false };
        assert!(!battery_due(&cfg, Some(&on_ac), Some(true), false, 101.0));
        assert!(!battery_due(&cfg, Some(&on_ac), Some(true), false, 159.0));
        assert!(battery_due(&cfg, Some(&on_ac), Some(true), false, 160.0), "once a minute on AC");
        assert!(battery_due(&cfg, Some(&on_ac), Some(false), false, 101.0), "pulled out: at once");
        assert!(battery_due(&cfg, Some(&on_ac), None, false, 101.0), "an adapter that stops answering is a change too");
        assert!(battery_due(&cfg, Some(&on_ac), Some(true), false, 50.0), "a clock that went back");
        let unplugged = BatteryRead { at: 100.0, on_ac: Some(false), percent: Some(60.0), follow_up: false };
        assert!(!battery_due(&cfg, Some(&unplugged), Some(false), false, 129.0));
        assert!(battery_due(&cfg, Some(&unplugged), Some(false), false, 130.0), "every 30 s on battery");
        assert!(battery_due(&cfg, Some(&unplugged), Some(true), false, 101.0), "plugged in: at once");
        let low = BatteryRead { percent: Some(20.0), ..unplugged };
        assert!(!battery_due(&cfg, Some(&low), Some(false), false, 109.0));
        assert!(battery_due(&cfg, Some(&low), Some(false), false, 110.0), "every 10 s when low");
        let unknown = BatteryRead { percent: None, ..unplugged };
        assert_eq!(battery_every(&cfg, &unknown), 10.0, "an unknown charge on battery is treated as low");
        // At most one read a second's worth of EC traffic, averaged: never
        // faster than every 10 s in any state.
        for last in [on_ac, unplugged, low, unknown, BatteryRead::default()] {
            assert!(battery_every(&cfg, &last) >= 10.0, "{last:?}");
        }
    }

    #[test]
    fn a_read_a_change_caused_is_read_once_more_soon_after() {
        let cfg = Config::default();
        let before = BatteryRead { at: 100.0, on_ac: Some(true), percent: Some(99.0), follow_up: false };
        // Unplugged at 130: read at once, and that read is an edge.
        assert!(battery_due(&cfg, Some(&before), Some(false), false, 130.0));
        assert!(battery_edge(Some(&before), Some(false), false));
        assert!(!battery_edge(None, Some(false), false), "the first read is not a change");
        assert!(!battery_edge(Some(&before), Some(true), false), "a routine read is not a change");
        let edge = BatteryRead { at: 130.0, on_ac: Some(false), percent: Some(99.0), follow_up: true };
        assert!(!battery_due(&cfg, Some(&edge), Some(false), false, 133.0));
        assert!(battery_due(&cfg, Some(&edge), Some(false), false, 134.0), "the follow-up, 4 s on");
        // The follow-up is not itself an edge: then the slow cadence again.
        assert!(!battery_edge(Some(&edge), Some(false), false));
        let settled = BatteryRead { at: 134.0, follow_up: false, ..edge };
        assert!(!battery_due(&cfg, Some(&settled), Some(false), false, 150.0));
        assert!(battery_due(&cfg, Some(&settled), Some(false), false, 164.0));
    }

    #[test]
    fn a_power_supply_uevent_reads_the_battery_soon_but_not_in_a_storm() {
        let cfg = Config::default();
        let last = BatteryRead { at: 100.0, on_ac: Some(true), percent: Some(99.0), follow_up: false };
        assert!(!battery_due(&cfg, Some(&last), Some(true), true, 101.0), "at most every 2 s");
        assert!(battery_due(&cfg, Some(&last), Some(true), true, 102.0), "a pack pulled out with BAT0 still listed");
        assert!(battery_edge(Some(&last), Some(true), true), "and once more after it");
        assert!(!battery_due(&cfg, Some(&last), Some(true), false, 102.0), "no uevent: the slow cadence");
    }

    #[test]
    fn smoothing_follows_slowly() {
        assert_eq!(smooth(None, Some(50.0), 1.0, 10.0), Some(50.0));
        let s = smooth(Some(50.0), Some(90.0), 1.0, 10.0).unwrap();
        assert!(s > 53.0 && s < 54.5, "{s}");
        assert_eq!(smooth(Some(50.0), None, 1.0, 10.0), Some(50.0));
    }
}

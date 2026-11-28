//! The controller: one pass reads the hardware, decides with `policy`, and
//! (in act mode) writes only what differs, journalling each original first.
//! In observe mode it decides and reports exactly the same, and writes
//! nothing at all.

use crate::hw::{self, BatteryReading, Hardware, Reading};
use crate::journal::{Journal, Kind};
use crate::policy::{self, BatteryRead, Being, Config, CpuCap, DreamLoop, Latch, Pressure, ResourceProfile};
use bus::Event;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::io;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// Decide and report, write nothing. The shipping default until one
    /// supervised session has been watched (task 2.8).
    Observe,
    /// Decide, report and write.
    Act,
}

impl Mode {
    pub fn name(self) -> &'static str {
        match self {
            Mode::Observe => "observe",
            Mode::Act => "act",
        }
    }

    pub fn from_name(name: &str) -> Option<Mode> {
        match name.trim() {
            "observe" => Some(Mode::Observe),
            "act" => Some(Mode::Act),
            _ => None,
        }
    }
}

/// How often to look for controls that appeared late, seconds.
const REDISCOVER: f64 = 60.0;
/// A failed package read keeps the last smoothed value this many seconds
/// before the controller goes blind (one flapping sensor read must not drop
/// the hot cap or the dream limiter).
const PACKAGE_HOLD: f64 = 5.0;
/// Blind after a reading over `fan_start_c` this recent: the fan floor is
/// never lowered, whatever the factory-floor target says.
const BLIND_FLOOR_HOLD: f64 = 120.0;

pub struct Care {
    root: PathBuf,
    pub cfg: Config,
    pub mode: Mode,
    pub hw: Hardware,
    journal: Journal,
    discovered_at: f64,
    last_at: Option<f64>,
    package: Option<f64>,
    /// When the package reads first failed (None while it reads), and when
    /// the smoothed package was last over `fan_start_c`.
    package_missed_since: Option<f64>,
    package_warm_at: Option<f64>,
    dream_pause: Latch,
    cpu_gate: Latch,
    dream: DreamLoop,
    fan_floor: Option<i64>,
    fan_stepped_at: f64,
    /// The last `/proc/stat` ticks, the smoothed busy share, and the
    /// sustained-load latch the fan floor follows.
    cpu_ticks: Option<(u64, u64)>,
    busy: Option<f64>,
    fan_load: Latch,
    charge_checked_at: Option<f64>,
    over_since: Option<f64>,
    honoured: Option<bool>,
    rapl_locked: bool,
    max_perf_clamp: Option<(String, String)>,
    said: HashSet<String>,
    /// Log lines waiting to be printed.
    pub notes: Vec<String>,
    /// What act mode would write (observe) or last wrote (act).
    pub wanted: HashMap<&'static str, String>,
    /// Act mode: the controls it wanted to change and could not (the
    /// journal or the write refused), so the report never shows them as held.
    pub not_applied: BTreeSet<&'static str>,
    /// Act mode: why the restore journal cannot be written, if it cannot.
    pub journal_error: Option<String>,
    pub cap: Option<CpuCap>,
    /// Observe-only WP-D8 advice. No actuator consumes these fields.
    pub resource_profile: Option<ResourceProfile>,
    pub pressure: Option<Pressure>,
    pub resource_priority: &'static str,
    pub resource_state_epoch: Option<u64>,
    pub fan_target: Option<i64>,
    pub factory_fan_min: Option<i64>,
    pub reading: Reading,
    /// The battery's own figures, read slowly (`policy::battery_due`), and
    /// when they were read.
    battery: BatteryReading,
    battery_read: Option<BatteryRead>,
    /// The power-change stamp as it was at the last battery read.
    power_stamp: Option<std::time::SystemTime>,
    /// How many times the battery's files have been read (tests, report).
    pub battery_reads: u64,
}

impl Care {
    pub fn new(root: &Path, journal_path: &Path, cfg: Config, mode: Mode) -> Care {
        let hw = Hardware::discover(root);
        let mut care = Care {
            root: root.to_path_buf(),
            cfg,
            mode,
            hw,
            journal: Journal::open(journal_path),
            discovered_at: 0.0,
            last_at: None,
            package: None,
            package_missed_since: None,
            package_warm_at: None,
            dream_pause: Latch::default(),
            cpu_gate: Latch::default(),
            dream: DreamLoop::default(),
            fan_floor: None,
            fan_stepped_at: f64::NEG_INFINITY,
            cpu_ticks: None,
            busy: None,
            fan_load: Latch::default(),
            charge_checked_at: None,
            over_since: None,
            honoured: None,
            rapl_locked: false,
            max_perf_clamp: None,
            said: HashSet::new(),
            notes: Vec::new(),
            wanted: HashMap::new(),
            not_applied: BTreeSet::new(),
            journal_error: None,
            cap: None,
            resource_profile: None,
            pressure: None,
            resource_priority: "state_unknown",
            resource_state_epoch: None,
            fan_target: None,
            factory_fan_min: None,
            reading: Reading::default(),
            battery: BatteryReading::default(),
            battery_read: None,
            power_stamp: None,
            battery_reads: 0,
        };
        for line in care.hw.survey() {
            care.notes.push(line);
        }
        if care.hw.fan_min.is_some() {
            let c = &care.cfg;
            care.notes.push(format!(
                "fan floor: the factory minimum until the package passes {:.0} C under sustained load (CPU busy over {:.0}%, smoothed over {:.0} s{}), rising to {:.0}% of the maximum by {:.0} C, where the SMC's own curve starts; over {:.0} C whatever the load; never lowered while listening",
                c.fan_start_c,
                c.fan_load_on * 100.0,
                c.fan_load_tau,
                if care.hw.stat.is_some() { "" } else { "; no CPU counters here, so the temperature alone" },
                c.fan_top_share * 100.0,
                c.fan_full_c,
                c.hot_package_c
            ));
        }
        if care.hw.battery.is_some() {
            let c = &care.cfg;
            care.notes.push(format!(
                "battery figures: read every {:.0} s on AC, {:.0} s on battery, {:.0} s when low, at once when the adapter or a power-supply uevent says it changed, and {:.0} s after that",
                c.battery_every, c.battery_unplugged_every, c.battery_low_every, c.battery_settle
            ));
        }
        if mode == Mode::Act {
            if let Err(e) = care.journal.check_writable() {
                care.notes.push(format!(
                    "act mode, but the restore journal {} cannot be written ({e}): nothing will be changed",
                    journal_path.display()
                ));
                care.journal_error = Some(e.to_string());
            }
        }
        care
    }

    fn once(&mut self, key: impl Into<String>, line: impl FnOnce() -> String) {
        if self.said.len() < 512 && self.said.insert(key.into()) {
            self.notes.push(line());
        }
    }

    /// Set a control to `value`, journalling the original first. Observe
    /// mode only notes what it would do.
    fn set(&mut self, label: &'static str, path: Option<PathBuf>, kind: Kind, value: String, current: Option<String>) -> bool {
        self.wanted.insert(label, value.clone());
        let Some(path) = path else { return false };
        if current.as_deref() == Some(value.as_str()) {
            if kind == Kind::MaxPerf {
                self.max_perf_clamp = None;
            }
            self.not_applied.remove(label);
            return false;
        }
        if kind == Kind::MaxPerf && self.max_perf_clamp.as_ref().is_some_and(|(requested, reported)| {
            requested == &value && current.as_deref() == Some(reported.as_str())
        }) {
            return false;
        }
        let done = self.write(label, &path, kind, &value, current);
        if self.mode == Mode::Act {
            if done {
                self.not_applied.remove(label);
            } else {
                self.not_applied.insert(label);
            }
        }
        done
    }

    fn write(&mut self, label: &'static str, path: &Path, kind: Kind, value: &str, current: Option<String>) -> bool {
        if self.mode == Mode::Observe {
            let shown = current.clone().unwrap_or_else(|| "?".into());
            self.once(format!("would:{label}:{value}"), || format!("observe: would set {label} from {shown} to {value}"));
            return false;
        }
        let Some(original) = current.or_else(|| hw::read_text(path)) else {
            self.once(format!("unreadable:{label}"), || format!("{label}: unreadable, so not changed"));
            return false;
        };
        if let Err(e) = self.journal.remember(path, kind, &original) {
            self.journal_error = Some(e.to_string());
            self.once(format!("journal:{label}"), || format!("{label}: could not journal the original ({e}), so not changed"));
            return false;
        }
        self.journal_error = None;
        match hw::write_value(path, value) {
            Ok(()) => {
                if kind == Kind::MaxPerf {
                    let actual = hw::read_text(path);
                    if actual.as_deref() != Some(value) {
                        let reported = actual.as_deref().unwrap_or("unavailable");
                        self.once(format!("read-back:{label}:{value}:{reported}"),
                                  || format!("{label}: requested {value}, kernel reports {reported}"));
                        self.max_perf_clamp = actual.map(|reported| (value.to_owned(), reported));
                        return false;
                    }
                    self.max_perf_clamp = None;
                }
                self.notes.push(format!("set {label} to {value} (was {original})"));
                true
            }
            Err(e) => {
                let refused = e.kind() == io::ErrorKind::PermissionDenied || matches!(e.raw_os_error(), Some(1 | 5 | 13 | 61));
                if kind == Kind::RaplPl1 && refused {
                    // Firmware locked the limit (EPERM, EIO, EACCES, ENODATA): stop asking.
                    self.rapl_locked = true;
                }
                self.once(format!("write:{label}:{}", e.kind()), || format!("{label}: could not write {value}: {e}"));
                false
            }
        }
    }

    /// One pass at `now` seconds.
    pub fn step(&mut self, being: &Being, now: f64) {
        if now - self.discovered_at >= REDISCOVER {
            self.discovered_at = now;
            let fresh = Hardware::discover(&self.root);
            if fresh != self.hw {
                self.notes.push("controls changed; surveying again".into());
                self.notes.extend(fresh.survey());
                self.hw = fresh;
            }
        }
        let r = self.read_hardware(now);
        let dt = self.last_at.map_or(1.0, |at| (now - at).max(0.0));
        self.last_at = Some(now);
        self.package = policy::smooth(self.package, r.package_c, dt, self.cfg.smoothing);
        if r.package_c.is_some() {
            self.package_missed_since = None;
        } else {
            // Keep the smoothed value for a short hold, then go blind.
            let since = *self.package_missed_since.get_or_insert(now);
            if now - since >= PACKAGE_HOLD {
                self.package = None;
            }
        }
        if self.package.is_some_and(|c| c > self.cfg.fan_start_c) {
            self.package_warm_at = Some(now);
        }
        let mut cfg = self.cfg.clone();
        cfg.dream_min_pct = cfg.dream_min_pct.clamp(r.min_perf.unwrap_or(1), 100);

        // CPU load, for the fan floor: the busy share since the last pass,
        // smoothed, and latched with hysteresis.
        let share = policy::busy_share(self.cpu_ticks, r.cpu_ticks);
        if r.cpu_ticks.is_some() {
            self.cpu_ticks = r.cpu_ticks;
        }
        self.busy = policy::smooth(self.busy, share, dt, cfg.fan_load_tau);
        let sustained = self.hw.stat.is_some().then(|| self.fan_load.update(self.busy, cfg.fan_load_on, cfg.fan_load_off));

        // Battery heat gates.
        let pause = self.dream_pause.update(r.battery_c, cfg.battery_pause_c, cfg.battery_resume_c);
        let hot = self.cpu_gate.update(r.battery_c, cfg.battery_cap_c, cfg.battery_resume_c);
        if pause {
            self.once("pause", || "battery over 40 C: dreams paused until it is under 37 C".into());
        } else {
            self.said.remove("pause");
        }

        // CPU caps by state. Night work (the dream, the diary, memory
        // upkeep) runs only while asleep with the lid closed, so that is
        // held at the target too, whether or not the mind says it dreams:
        // idle and cool, the loop opens straight up to 100%.
        let dreaming = (being.dreaming || being.night()) && !pause;
        let dream_pct = if dreaming {
            Some(self.dream.update(self.package, cfg.dream_target_c, cfg.dream_min_pct, dt))
        } else {
            self.dream.reset();
            None
        };
        let mut cap = policy::cpu_cap(&cfg, being, self.package, r.on_ac, r.battery_present, hot, dream_pct);
        cap.max_perf_pct = cap.max_perf_pct.max(r.min_perf.unwrap_or(1));
        let turbo_now = r.no_turbo.map(|v| if v { "1" } else { "0" }.to_string());
        self.set("no_turbo", self.hw.no_turbo.clone(), Kind::NoTurbo, if cap.no_turbo { "1" } else { "0" }.into(), turbo_now);
        self.set("max_perf_pct", self.hw.max_perf.clone(), Kind::MaxPerf, cap.max_perf_pct.to_string(), r.max_perf.map(|v| v.to_string()));
        match (cap.rapl_w, self.rapl_locked) {
            (Some(watts), false) => {
                let uw = ((watts * 1e6) as i64).to_string();
                self.set("rapl_pl1", self.hw.rapl_pl1.clone(), Kind::RaplPl1, uw, r.rapl_pl1_uw.map(|v| v.to_string()));
            }
            (Some(_), true) => self.once("rapl-locked", || "RAPL PL1 is locked by firmware; relying on the P-state cap".into()),
            (None, _) => {
                // No limit wanted: put back the original if we changed it.
                if let (Some(path), false) = (self.hw.rapl_pl1.clone(), self.rapl_locked) {
                    if let Some(original) = self.journal.original(&path) {
                        self.set("rapl_pl1", Some(path), Kind::RaplPl1, original, r.rapl_pl1_uw.map(|v| v.to_string()));
                    }
                }
                self.wanted.remove("rapl_pl1");
                self.not_applied.remove("rapl_pl1");
            }
        }
        self.cap = Some(cap);

        // Fan floor: fan1_min only, never manual.
        if let (Some(path), Some(current)) = (self.hw.fan_min.clone(), r.fan_min) {
            // After a restart the journal, not the raised value, is the factory floor.
            let factory = self.journal.original(&path).and_then(|v| v.parse().ok()).unwrap_or(current);
            self.factory_fan_min = Some(factory);
            let floor = *self.fan_floor.get_or_insert(current);
            let loaded = policy::fan_loaded(&cfg, self.package, sustained);
            let target = policy::fan_target(&cfg, self.package, loaded, factory, r.fan_max);
            self.fan_target = Some(target);
            if now - self.fan_stepped_at >= cfg.fan_step_every {
                let mut next = policy::fan_next(&cfg, floor, target, being.listening());
                // Blind just after a hot reading: never lower the floor.
                let recently_hot = self.package_warm_at.is_some_and(|at| now - at < BLIND_FLOOR_HOLD);
                if self.package.is_none() && recently_hot {
                    next = next.max(floor);
                }
                if next != floor {
                    self.fan_stepped_at = now;
                    self.fan_floor = Some(next);
                }
            }
            let floor = self.fan_floor.unwrap_or(current);
            self.set("fan1_min", Some(path), Kind::FanMin, floor.to_string(), Some(current.to_string()));
        }

        // Charge limit: re-asserted at start and every ten minutes.
        if let Some(limit) = cfg.charge_limit {
            let due = self.charge_checked_at.is_none_or(|at| now - at >= cfg.charge_every);
            if due {
                self.charge_checked_at = Some(now);
                match self.hw.charge.clone() {
                    Some((path, _)) => {
                        if r.charge_limit != Some(limit) && r.charge_limit.is_some() && self.mode == Mode::Act {
                            self.notes.push(format!("charge limit reads {:?}; re-asserting {limit}", r.charge_limit));
                        }
                        self.set("charge_limit", Some(path), Kind::ChargeLimit, limit.to_string(), r.charge_limit.map(|v| v.to_string()));
                    }
                    None => self.once("no-charge", || "charge limit wanted but no control exists (task 2.7); nothing done".into()),
                }
            }
            self.judge_cap(limit, &r, now);
        }
        self.reading = r;
        self.resource_profile = policy::resource_profile(being);
        self.resource_state_epoch = self.resource_profile.and(being.state_epoch);
        self.pressure = policy::pressure(&cfg, self.package, self.reading.battery_c);
        self.resource_priority = match (self.pressure, self.reading.on_ac, being.lid_closed) {
            (Some(Pressure::Hot | Pressure::Critical), _, _) => "thermal",
            (_, Some(false), _) => "power_source",
            (_, None, _) => "power_unknown",
            (_, _, Some(true)) => "lid",
            _ if self.resource_profile.is_some() => "cognitive",
            _ => "state_unknown",
        };
    }

    /// One reading: the cheap controls every pass, the battery's own files
    /// only when `policy::battery_due` says so or the battery changed. A
    /// read that a change caused (the battery rediscovered, the adapter, a
    /// power-supply uevent) is followed by one more `battery_settle` later.
    fn read_hardware(&mut self, now: f64) -> Reading {
        let on_ac = hw::read_ac(&self.hw);
        let moved = self.battery.dir != self.hw.battery;
        let stamp = hw::power_stamp(&self.root);
        let poked = stamp != self.power_stamp;
        let last = self.battery_read.as_ref();
        if moved || policy::battery_due(&self.cfg, last, on_ac, poked, now) {
            let follow_up = (moved && last.is_some()) || policy::battery_edge(last, on_ac, poked);
            self.battery = hw::read_battery(&self.hw);
            self.battery_reads += 1;
            self.battery_read = Some(BatteryRead { at: now, on_ac, percent: self.battery.percent, follow_up });
            self.power_stamp = stamp;
        }
        hw::read_fast(&self.hw, &self.battery)
    }

    /// Is the cap honoured? Judged only while the control really reads the
    /// limit (in act mode once it is written; in observe mode only if the
    /// firmware already holds it), so a cap that was never set is unknown,
    /// never "ignored". On AC, well over the limit and still charging for
    /// `honour_after` means not honoured; resting at the cap means honoured.
    fn judge_cap(&mut self, limit: i64, r: &Reading, now: f64) {
        let in_force = self.hw.charge.is_some() && r.charge_limit == Some(limit);
        if !in_force {
            if self.honoured.is_some() {
                self.notes.push(format!("the charge limit reads {:?}, not {limit}; whether it holds is unknown", r.charge_limit));
            }
            self.honoured = None;
            self.over_since = None;
            return;
        }
        let margin = self.cfg.honour_margin;
        let over = r.on_ac == Some(true)
            && r.percent.is_some_and(|p| p >= limit as f64 + margin)
            && r.status.as_deref() == Some("Charging");
        if over {
            let since = *self.over_since.get_or_insert(now);
            if now - since >= self.cfg.honour_after && self.honoured != Some(false) {
                self.honoured = Some(false);
                self.notes.push(format!(
                    "the charge limit of {limit}% is not being honoured: still charging at {:?}% ({} mode)",
                    r.percent,
                    self.mode.name()
                ));
            }
        } else {
            self.over_since = None;
            let resting = matches!(r.status.as_deref(), Some("Not charging" | "Full"));
            if r.on_ac == Some(true) && resting && r.percent.is_some_and(|p| (p - limit as f64).abs() <= margin) {
                if self.honoured != Some(true) {
                    self.notes.push(format!("the charge limit of {limit}% is holding"));
                }
                self.honoured = Some(true);
            }
        }
    }

    /// Whether the cap holds: Some(true) resting at it, Some(false) charging
    /// past it, None when unknown (no cap chosen, no control, not in force).
    pub fn cap_honoured(&self) -> Option<bool> {
        self.cfg.charge_limit.and(self.honoured)
    }

    /// Dreams may run (not paused by battery heat).
    pub fn dream_allowed(&self) -> bool {
        !self.dream_pause.on
    }

    /// The report for the bus.
    pub fn report(&self) -> Event {
        let cap = self.cap.clone();
        let r = &self.reading;
        let round = |v: Option<f64>| v.map(|v| (v * 10.0).round() / 10.0);
        let mut ev = Event::new("body.care")
            .with("mode", self.mode.name())
            .with("package_c", round(self.package))
            .with("battery_c", round(r.battery_c))
            .with("dream_allowed", self.dream_allowed())
            .with("battery_hot", self.cpu_gate.on)
            .with("no_turbo", cap.as_ref().map(|c| c.no_turbo))
            .with("max_perf_pct", cap.as_ref().map(|c| c.max_perf_pct))
            .with("rapl_w", cap.as_ref().and_then(|c| c.rapl_w))
            .with("cpu_reasons", cap.as_ref().map(|c| c.reasons.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap_or_default())
            // WP-D8 preparation: descriptive, short-lived advice only. It
            // never selects performance and is not connected to an actuator.
            .with("resource_profile", self.resource_profile.map(ResourceProfile::name))
            .with("resource_pressure", self.pressure.map(Pressure::name))
            .with("resource_priority", self.resource_priority)
            .with("resource_state_epoch", self.resource_state_epoch)
            .with("resource_observe_only", true)
            .with("resource_ttl_ms", if self.resource_profile.is_some() { 12_000 } else { 0 })
            .with("performance_eligible", false)
            .with("fan_rpm", r.fan_rpm)
            .with("fan_floor", self.fan_floor)
            .with("fan_target", self.fan_target)
            .with("fan_factory_min", self.factory_fan_min)
            // The smoothed busy share of all CPUs (0-1; null: no counters
            // yet) and whether it counts as sustained load for the fan.
            .with("cpu_busy", self.busy.map(|v| (v * 100.0).round() / 100.0))
            .with("fan_load", self.hw.stat.as_ref().map(|_| self.fan_load.on))
            // Always present, so "how's your battery health" never guesses:
            // the owner's chosen cap (null: none) and whether it holds.
            .with("charge_limit", self.cfg.charge_limit)
            .with("cap_honoured", self.cap_honoured())
            // Act mode with every wanted control really set. Observe mode,
            // a journal that cannot be written or a refused write: false,
            // and the refused ones are named, so no cap is claimed that is
            // not in force.
            .with("acting", self.mode == Mode::Act && self.not_applied.is_empty() && self.journal_error.is_none())
            .with("not_applied", self.not_applied.iter().copied().collect::<Vec<_>>());
        if let Some(e) = &self.journal_error {
            ev = ev.with("journal_error", e.as_str());
        }
        if self.cfg.charge_limit.is_some() {
            ev = ev
                .with("charge_limit_reads", r.charge_limit)
                .with("charge_limit_control", self.hw.charge.as_ref().map(|(_, k)| k.name()))
                .with("charge_limit_honoured", self.honoured);
        }
        ev
    }

    /// Undo every change but the charge limit (kept: the owner's choice).
    pub fn restore(&mut self) -> Vec<String> {
        self.journal.restore(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Tree(PathBuf);

    impl Tree {
        fn new(tag: &str) -> Tree {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let n = NEXT.fetch_add(1, Ordering::SeqCst);
            let root = std::env::temp_dir().join(format!("bodycare-{}-{tag}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).unwrap();
            Tree(root)
        }

        fn file(&self, path: &str, data: &str) {
            let path = self.0.join(path);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, data).unwrap();
        }

        fn get(&self, path: &str) -> String {
            fs::read_to_string(self.0.join(path)).unwrap().trim().to_string()
        }

        fn journal(&self) -> PathBuf {
            self.0.join("run/being/bodycare-restore.json")
        }
    }

    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const SMC: &str = "sys/devices/platform/applesmc.768";
    const PSTATE: &str = "sys/devices/system/cpu/intel_pstate";
    const RAPL: &str = "sys/class/powercap/intel-rapl:0";

    /// A MacBookPro9,2 on Linux 6.18 with the applesmc charge-limit patch.
    fn macbook(tag: &str) -> Tree {
        let t = Tree::new(tag);
        t.file("sys/class/power_supply/ADP1/type", "Mains\n");
        t.file("sys/class/power_supply/ADP1/online", "1\n");
        t.file("sys/class/power_supply/BAT0/type", "Battery\n");
        t.file("sys/class/power_supply/BAT0/present", "1\n");
        t.file("sys/class/power_supply/BAT0/status", "Charging\n");
        t.file("sys/class/power_supply/BAT0/capacity", "70\n");
        t.file(&format!("{SMC}/battery_charge_limit"), "100\n");
        t.file(&format!("{SMC}/fan1_min"), "2000\n");
        t.file(&format!("{SMC}/fan1_max"), "6200\n");
        t.file(&format!("{SMC}/fan1_input"), "2001\n");
        t.file(&format!("{SMC}/fan1_manual"), "0\n");
        t.file(&format!("{SMC}/temp1_label"), "TB0T\n");
        t.file(&format!("{SMC}/temp1_input"), "31000\n");
        t.file(&format!("{SMC}/temp2_label"), "TB1T\n");
        t.file(&format!("{SMC}/temp2_input"), "-127000\n");
        t.file(&format!("{SMC}/temp3_label"), "TC0P\n");
        t.file(&format!("{SMC}/temp3_input"), "55000\n");
        // 6.18: the hwmon device has no name of its own; coretemp does.
        t.file("sys/class/hwmon/hwmon3/name", "coretemp\n");
        t.file("sys/class/hwmon/hwmon3/temp1_label", "Package id 0\n");
        t.file("sys/class/hwmon/hwmon3/temp1_input", "50000\n");
        t.file("sys/class/hwmon/hwmon3/temp2_label", "Core 0\n");
        t.file("sys/class/hwmon/hwmon3/temp2_input", "99000\n");
        t.file(&format!("{PSTATE}/status"), "passive\n");
        t.file(&format!("{PSTATE}/no_turbo"), "0\n");
        t.file(&format!("{PSTATE}/max_perf_pct"), "100\n");
        // The 35 W / 43.75 W limits below are synthetic PC fixture values.
        // The target MacBook reported its factory package limits at 100 W / 125 W.
        t.file(&format!("{RAPL}/name"), "package-0\n");
        t.file(&format!("{RAPL}/constraint_0_name"), "long_term\n");
        t.file(&format!("{RAPL}/constraint_0_power_limit_uw"), "35000000\n");
        t.file(&format!("{RAPL}/constraint_1_name"), "short_term\n");
        t.file(&format!("{RAPL}/constraint_1_power_limit_uw"), "43750000\n");
        t.file("sys/class/powercap/intel-rapl:0:0/name", "core\n");
        t
    }

    fn being(state: &str, lid_closed: bool) -> Being {
        Being { state: Some(state.into()), lid_closed: Some(lid_closed), dreaming: false, state_fresh: true, state_epoch: Some(1) }
    }

    fn act(t: &Tree, cfg: Config) -> Care {
        Care::new(&t.0, &t.journal(), cfg, Mode::Act)
    }

    #[test]
    fn the_macbook_is_found_with_its_package_channel_and_battery_sensors() {
        let t = macbook("find");
        let hw = Hardware::discover(&t.0);
        assert_eq!(hw.package.len(), 1, "only \"Package id 0\"");
        assert!(hw.package_labelled);
        assert_eq!(hw.charge.as_ref().map(|(_, k)| *k), Some(hw::ChargeKind::Applesmc));
        let r = hw::read(&hw);
        assert_eq!(r.package_c, Some(50.0));
        assert_eq!(r.battery_c, Some(31.0), "the junk TB1T is skipped");
        assert_eq!((r.on_ac, r.battery_present, r.percent), (Some(true), Some(true), Some(70.0)));
        assert!(hw.rapl_pl1.as_ref().unwrap().ends_with("constraint_0_power_limit_uw"));
        // The kernel's standard threshold wins when a driver offers it.
        t.file("sys/class/power_supply/BAT0/charge_control_end_threshold", "100\n");
        assert_eq!(Hardware::discover(&t.0).charge.map(|(_, k)| k), Some(hw::ChargeKind::EndThreshold));
    }

    #[test]
    fn observe_mode_decides_everything_and_writes_nothing() {
        let t = macbook("observe");
        let before = ["fan1_min", "fan1_manual", "battery_charge_limit"].map(|f| t.get(&format!("{SMC}/{f}")));
        t.file("sys/class/hwmon/hwmon3/temp1_input", "90000\n");
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        let mut care = Care::new(&t.0, &t.journal(), Config { charge_limit: Some(80), ..Config::default() }, Mode::Observe);
        for s in 0..120 {
            care.step(&being("asleep", true), f64::from(s));
        }
        assert_eq!(["fan1_min", "fan1_manual", "battery_charge_limit"].map(|f| t.get(&format!("{SMC}/{f}"))), before);
        assert_eq!((t.get(&format!("{PSTATE}/no_turbo")), t.get(&format!("{PSTATE}/max_perf_pct"))), ("0".into(), "100".into()));
        assert!(!t.journal().exists());
        let notes = care.notes.join("\n");
        assert!(notes.contains("observe: would set no_turbo from 0 to 1"), "{notes}");
        assert!(notes.contains("observe: would set charge_limit from 100 to 80"), "{notes}");
        let report = care.report();
        assert_eq!(report.str("mode"), Some("observe"));
        assert_eq!(report.bool("no_turbo"), Some(true));
        assert!(report.f64("fan_floor").unwrap() > 2000.0, "it reports the floor it would hold");
    }

    #[test]
    fn act_mode_steps_the_fan_floor_and_never_touches_manual_mode() {
        let t = macbook("fan");
        t.file("sys/class/hwmon/hwmon3/temp1_input", "90000\n");
        let mut care = act(&t, Config::default());
        let floor = |t: &Tree| t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap();
        care.step(&being("thinking", false), 0.0);
        assert_eq!(floor(&t), 2250, "one step at once");
        care.step(&being("thinking", false), 5.0);
        assert_eq!(floor(&t), 2250, "no faster than one step per 10 s");
        care.step(&being("thinking", false), 10.0);
        assert_eq!(floor(&t), 2500);
        // The owner is talking: the floor still rises toward the target (the
        // package is hot) but is never lowered.
        for s in 11..60 {
            care.step(&being("listening", false), f64::from(s));
        }
        assert_eq!(floor(&t), 2850, "raised while listening, up to the 90 C target");
        // The lid closes, attending: no change at the target.
        for s in 60..400 {
            care.step(&being("attending", true), f64::from(s));
        }
        assert_eq!(floor(&t), 2850, "90 C: 5/12 of the way from 2000 to the 4000 top");
        assert_eq!(t.get(&format!("{SMC}/fan1_manual")), "0", "never manual mode");
        // Cool again: back down by steps to the factory floor.
        t.file("sys/class/hwmon/hwmon3/temp1_input", "45000\n");
        for s in 400..900 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(floor(&t), 2000);
    }

    /// `/proc/stat` for a four-thread CPU that has been `busy` (0-1) since
    /// the last write, `secs` seconds of ticks (100 a second per thread).
    struct Stat {
        busy: u64,
        total: u64,
    }

    impl Stat {
        fn advance(&mut self, t: &Tree, secs: f64, busy: f64) {
            let ticks = (secs * 400.0) as u64;
            let worked = (ticks as f64 * busy) as u64;
            self.busy += worked;
            self.total += ticks;
            // user, nice (the model runs niced), system, idle, iowait.
            let (user, nice) = (self.busy / 3, self.busy - self.busy / 3 - self.busy / 10);
            let system = self.busy - user - nice;
            let idle = (self.total - self.busy) * 9 / 10;
            let iowait = self.total - self.busy - idle;
            t.file("proc/stat", &format!("cpu {user} {nice} {system} {idle} {iowait} 0 0 0 0 0\n"));
        }
    }

    /// The package as measured under sustained model load on 25 November
    /// 2026, from a warm idle: 80 C, 95 C after 61 s, 100-101 C after about
    /// 110 s, then flat (TjMax 105, no throttling).
    fn package_under_load(secs: f64) -> f64 {
        let points = [(0.0, 80.0), (61.0, 95.0), (110.0, 100.5), (600.0, 101.0)];
        let i = points.windows(2).position(|w| secs <= w[1].0).unwrap_or(points.len() - 2);
        let ((t0, c0), (t1, c1)) = (points[i], points[i + 1]);
        c0 + (c1 - c0) * ((secs - t0) / (t1 - t0)).clamp(0.0, 1.0)
    }

    /// What the SMC's own curve did in the same runs: 2000 rpm until about
    /// 97 C, then up (2105 at 98 C, 2772-2990 at 100 C).
    fn smc_rpm(package_c: f64) -> i64 {
        if package_c < 97.0 { 2000 } else { 2000 + ((package_c - 97.0) * 300.0) as i64 }
    }

    #[test]
    fn a_warm_idle_never_asks_for_more_fan() {
        // Observe mode on the laptop asked for "fan1_min from 2000 to 3300",
        // rising to 4450, with the package at 71-82 C and nothing working.
        let t = macbook("warm-idle");
        let mut stat = Stat { busy: 0, total: 0 };
        stat.advance(&t, 1.0, 0.2);
        let mut care = Care::new(&t.0, &t.journal(), Config::default(), Mode::Observe);
        assert!(care.notes.iter().any(|n| n.contains("fan floor: the factory minimum until the package passes 85 C")), "{:?}", care.notes);
        for s in 0..1800u32 {
            // An idle that wanders 71-82 C, with the voice and face at 15-22%.
            let temp = 76.5 + 5.5 * (f64::from(s) / 97.0).sin();
            t.file("sys/class/hwmon/hwmon3/temp1_input", &format!("{}\n", (temp * 1000.0) as i64));
            stat.advance(&t, 1.0, 0.185 + 0.035 * (f64::from(s) / 13.0).sin());
            let state = if s % 300 < 20 { "listening" } else { "idle" };
            care.step(&being(state, false), f64::from(s));
            assert_eq!(care.fan_target, Some(2000), "at {s} s, {temp:.1} C");
        }
        let notes = care.notes.join("\n");
        assert!(!notes.contains("would set fan1_min"), "{notes}");
        let report = care.report();
        assert_eq!(report.bool("fan_load"), Some(false));
        let busy = report.f64("cpu_busy").unwrap();
        assert!((0.15..0.23).contains(&busy), "{busy}");
    }

    #[test]
    fn sustained_model_load_adds_air_before_the_smc_would_and_one_reply_does_not() {
        let t = macbook("load");
        let floor = |t: &Tree| t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap();
        let mut stat = Stat { busy: 0, total: 0 };
        stat.advance(&t, 1.0, 0.18);
        let mut care = act(&t, Config::default());
        let mut now = 0.0;
        let mut step = |care: &mut Care, stat: &mut Stat, temp: f64, busy: f64, state: &str| {
            t.file("sys/class/hwmon/hwmon3/temp1_input", &format!("{}\n", (temp * 1000.0) as i64));
            stat.advance(&t, 1.0, busy);
            care.step(&being(state, false), now);
            now += 1.0;
        };
        // Warm idle for a minute, then one reply: four seconds of the model at
        // 92-94 C (the die heats in seconds) and back.
        for _ in 0..60 {
            step(&mut care, &mut stat, 80.0, 0.18, "idle");
        }
        for _ in 0..4 {
            step(&mut care, &mut stat, 93.0, 0.68, "thinking");
        }
        for _ in 0..60 {
            step(&mut care, &mut stat, 80.0, 0.18, "speaking");
        }
        assert_eq!(floor(&t), 2000, "one reply never spins the fan");
        assert!(!care.notes.iter().any(|n| n.contains("set fan1_min")), "{:?}", care.notes);

        // A long read: the model flat out, the package on the measured curve.
        let mut first_raise = None;
        let mut most = 0;
        for s in 0..300u32 {
            let temp = package_under_load(f64::from(s));
            step(&mut care, &mut stat, temp, 0.68, "speaking");
            let f = floor(&t);
            if f > 2000 && first_raise.is_none() {
                first_raise = Some((s, temp));
            }
            most = most.max(f);
        }
        let (at, temp) = first_raise.expect("sustained load raises the floor");
        assert!(temp < 95.0 && smc_rpm(temp) == 2000, "it adds air before the SMC would: {at} s, {temp:.1} C");
        assert!(at >= 15, "not before the load has lasted ({at} s)");
        assert!(most <= 4000, "never over the top: {most}");
        assert!(floor(&t) >= smc_rpm(package_under_load(299.0)), "at the plateau it is at least what the SMC runs");
        assert_eq!(care.report().bool("fan_load"), Some(true));

        // The read ends: idle again, and the floor steps back to the factory.
        for _ in 0..400 {
            step(&mut care, &mut stat, 78.0, 0.18, "idle");
        }
        assert_eq!(floor(&t), 2000);
        assert_eq!(care.report().bool("fan_load"), Some(false));
    }

    #[test]
    fn a_hot_package_raises_the_floor_whatever_the_load() {
        // Heat from somewhere the CPU counters do not see (the GPU, a blocked
        // vent): over 92 C the temperature alone decides.
        let t = macbook("hot");
        let mut stat = Stat { busy: 0, total: 0 };
        stat.advance(&t, 1.0, 0.1);
        let mut care = act(&t, Config::default());
        for s in 0..30u32 {
            t.file("sys/class/hwmon/hwmon3/temp1_input", "95000\n");
            stat.advance(&t, 1.0, 0.1);
            care.step(&being("idle", false), f64::from(s));
        }
        assert!(t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap() > 2000);
        assert_eq!(care.report().bool("fan_load"), Some(false), "not load: heat");
    }

    #[test]
    fn a_hot_package_raises_the_floor_even_while_listening() {
        let t = macbook("hot-listening");
        let mut stat = Stat { busy: 0, total: 0 };
        stat.advance(&t, 1.0, 0.1);
        let mut care = act(&t, Config::default());
        for s in 0..30u32 {
            t.file("sys/class/hwmon/hwmon3/temp1_input", "98000\n");
            stat.advance(&t, 1.0, 0.1);
            care.step(&being("attending", false), f64::from(s));
        }
        assert!(t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap() > 2000);
    }

    #[test]
    fn one_failed_package_read_keeps_the_cap_and_the_floor() {
        let t = macbook("flap");
        let mut care = act(&t, Config::default());
        for s in 0..40u32 {
            t.file("sys/class/hwmon/hwmon3/temp1_input", "98000\n");
            care.step(&being("idle", false), f64::from(s));
        }
        let raised = t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap();
        assert!(raised > 2000);
        // A read error for 3 s: still capped, floor not lowered.
        t.file("sys/class/hwmon/hwmon3/temp1_input", "junk\n");
        for s in 40..43u32 {
            care.step(&being("idle", false), f64::from(s));
            assert_eq!(care.cap.as_ref().unwrap().max_perf_pct, 70, "hot cap held at {s}");
        }
        // Blind for a long time right after a hot reading: the cap goes, the
        // floor is held until the blind hold is over, never stepped down early.
        for s in 43..150u32 {
            care.step(&being("idle", false), f64::from(s));
            assert!(t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap() >= raised, "lowered while blind at {s}");
        }
        assert!(care.package.is_none());
    }

    #[test]
    fn restore_puts_back_every_original_even_after_a_crash() {
        let t = macbook("restore");
        t.file("sys/class/hwmon/hwmon3/temp1_input", "90000\n");
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        let mut care = act(&t, Config { charge_limit: Some(80), ..Config::default() });
        for s in 0..60 {
            care.step(&being("asleep", true), f64::from(s));
        }
        assert_eq!(t.get(&format!("{PSTATE}/no_turbo")), "1");
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "80");
        assert!(t.get(&format!("{SMC}/fan1_min")).parse::<i64>().unwrap() > 2000);
        drop(care); // killed: no restore of its own

        // A restarted controller still knows the factory floor.
        let mut again = act(&t, Config { charge_limit: Some(80), ..Config::default() });
        again.step(&being("asleep", true), 0.0);
        assert_eq!(again.factory_fan_min, Some(2000));
        drop(again);

        // ExecStopPost=--restore
        let mut journal = Journal::open(&t.journal());
        let lines = journal.restore(false).join("\n");
        assert_eq!(t.get(&format!("{SMC}/fan1_min")), "2000");
        assert_eq!(t.get(&format!("{PSTATE}/no_turbo")), "0");
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "80", "the owner's cap is kept");
        assert!(lines.contains("kept the charge limit"), "{lines}");
        assert!(t.journal().exists(), "the kept entry stays journalled");
        let mut journal = Journal::open(&t.journal());
        journal.restore(true);
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "100");
        assert!(!t.journal().exists(), "nothing left to undo");
    }

    #[test]
    fn the_charge_limit_is_reasserted_and_a_cap_that_is_ignored_is_noticed() {
        let t = macbook("charge");
        let cfg = Config { charge_limit: Some(80), ..Config::default() };
        let mut care = act(&t, cfg);
        care.step(&being("idle", false), 0.0);
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "80");
        // An SMC reset clears it: put back within ten minutes.
        t.file(&format!("{SMC}/battery_charge_limit"), "100\n");
        care.step(&being("idle", false), 300.0);
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "100");
        care.step(&being("idle", false), 600.0);
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "80");
        // Still charging at 86% on AC for two hours: not honoured.
        t.file("sys/class/power_supply/BAT0/capacity", "86\n");
        for m in 0..=121 {
            care.step(&being("idle", false), 700.0 + f64::from(m) * 60.0);
        }
        assert_eq!(care.report().bool("charge_limit_honoured"), Some(false));
        assert!(care.notes.iter().any(|n| n.contains("not being honoured")));
        // Resting at the cap: honoured.
        t.file("sys/class/power_supply/BAT0/status", "Not charging\n");
        t.file("sys/class/power_supply/BAT0/capacity", "81\n");
        care.step(&being("idle", false), 99_999.0);
        assert_eq!(care.report().bool("charge_limit_honoured"), Some(true));
        assert_eq!(care.report().bool("cap_honoured"), Some(true));
        assert_eq!(care.report().data["charge_limit"].as_i64(), Some(80));
        // An SMC reset puts it back to 100: unknown again until re-asserted.
        t.file(&format!("{SMC}/battery_charge_limit"), "100\n");
        care.step(&being("idle", false), 100_000.0);
        assert!(care.report().data["cap_honoured"].is_null());
        // No limit chosen: never touched, and nothing claimed.
        let t = macbook("charge-none");
        let mut care = act(&t, Config::default());
        care.step(&being("idle", false), 0.0);
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "100");
        let report = care.report();
        assert!(report.data["charge_limit"].is_null() && report.data["cap_honoured"].is_null());
        assert!(report.data.get("charge_limit_reads").is_none());
    }

    #[test]
    fn observe_mode_notices_a_cap_the_firmware_holds_but_ignores() {
        // Observe mode writes nothing, so a control at 100 is a cap never
        // set: charging to 95% is not "ignoring" it.
        let t = macbook("observe-cap");
        t.file("sys/class/power_supply/BAT0/capacity", "95\n");
        let cfg = Config { charge_limit: Some(80), ..Config::default() };
        let mut care = Care::new(&t.0, &t.journal(), cfg.clone(), Mode::Observe);
        for m in 0..=130 {
            care.step(&being("idle", false), f64::from(m) * 60.0);
        }
        assert!(care.report().data["cap_honoured"].is_null(), "never set, so unknown");
        assert!(!care.notes.iter().any(|n| n.contains("not being honoured")));
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "100", "observe writes nothing");

        // The SMC already holds 80 (set before), yet it keeps charging: noticed and published.
        t.file(&format!("{SMC}/battery_charge_limit"), "80\n");
        let mut care = Care::new(&t.0, &t.journal(), cfg.clone(), Mode::Observe);
        for m in 0..=110 {
            care.step(&being("idle", false), f64::from(m) * 60.0);
        }
        assert!(care.report().data["cap_honoured"].is_null(), "two hours are needed, not 110 minutes");
        for m in 111..=125 {
            care.step(&being("idle", false), f64::from(m) * 60.0);
        }
        let report = care.report();
        assert_eq!(report.bool("cap_honoured"), Some(false));
        assert_eq!(report.bool("charge_limit_honoured"), Some(false), "the older field agrees");
        assert!(care.notes.iter().any(|n| n.contains("not being honoured") && n.contains("observe mode")), "{:?}", care.notes);
        // A dip under the margin restarts the clock but does not clear the verdict.
        t.file("sys/class/power_supply/BAT0/capacity", "83\n");
        care.step(&being("idle", false), 126.0 * 60.0);
        assert_eq!(care.report().bool("cap_honoured"), Some(false));
        // Off AC it proves nothing either way.
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        t.file("sys/class/power_supply/BAT0/status", "Discharging\n");
        care.step(&being("idle", false), 127.0 * 60.0);
        assert_eq!(care.report().bool("cap_honoured"), Some(false));
        // Back on AC and resting at 80: it holds after all.
        t.file("sys/class/power_supply/ADP1/online", "1\n");
        t.file("sys/class/power_supply/BAT0/status", "Not charging\n");
        t.file("sys/class/power_supply/BAT0/capacity", "80\n");
        care.step(&being("idle", false), 128.0 * 60.0);
        assert_eq!(care.report().bool("cap_honoured"), Some(true));
        assert!(care.notes.iter().any(|n| n.contains("is holding")));

        // No control at all: unknown, whatever the battery does.
        let t = macbook("observe-nocontrol");
        std::fs::remove_file(t.0.join(format!("{SMC}/battery_charge_limit"))).unwrap();
        t.file("sys/class/power_supply/BAT0/status", "Not charging\n");
        t.file("sys/class/power_supply/BAT0/capacity", "80\n");
        let mut care = Care::new(&t.0, &t.journal(), cfg, Mode::Observe);
        care.step(&being("idle", false), 0.0);
        assert!(care.report().data["cap_honoured"].is_null());
    }

    #[test]
    fn without_a_battery_rapl_limits_the_draw_and_a_locked_limit_is_left() {
        let t = macbook("rapl");
        std::fs::remove_dir_all(t.0.join("sys/class/power_supply/BAT0")).unwrap();
        let mut care = act(&t, Config::default());
        care.step(&being("thinking", false), 0.0);
        assert_eq!(t.get(&format!("{RAPL}/constraint_0_power_limit_uw")), "25000000");
        assert_eq!(t.get(&format!("{PSTATE}/max_perf_pct")), "85");
        // A pack pulled out between discoveries is not still "present".
        let t2 = macbook("rapl-pulled");
        let mut pulled = act(&t2, Config::default());
        pulled.step(&being("thinking", false), 0.0);
        assert_eq!(t2.get(&format!("{PSTATE}/max_perf_pct")), "100");
        std::fs::remove_dir_all(t2.0.join("sys/class/power_supply/BAT0")).unwrap();
        pulled.step(&being("thinking", false), 1.0);
        assert_eq!(t2.get(&format!("{PSTATE}/max_perf_pct")), "85", "capped at once, not after the next survey");
        // The battery comes back: the original limit returns.
        t.file("sys/class/power_supply/BAT0/type", "Battery\n");
        t.file("sys/class/power_supply/BAT0/capacity", "50\n");
        care.step(&being("thinking", false), 61.0);
        assert_eq!(t.get(&format!("{RAPL}/constraint_0_power_limit_uw")), "35000000");
        assert_eq!(t.get(&format!("{PSTATE}/max_perf_pct")), "100");

        // Locked by firmware: a write that fails is not retried.
        let t = macbook("rapl-locked");
        std::fs::remove_dir_all(t.0.join("sys/class/power_supply/BAT0")).unwrap();
        let limit = t.0.join(format!("{RAPL}/constraint_0_power_limit_uw"));
        let mut perms = std::fs::metadata(&limit).unwrap().permissions();
        std::os::unix::fs::PermissionsExt::set_mode(&mut perms, 0o444);
        std::fs::set_permissions(&limit, perms).unwrap();
        let mut care = act(&t, Config::default());
        care.step(&being("thinking", false), 0.0);
        care.step(&being("thinking", false), 1.0);
        if std::fs::OpenOptions::new().write(true).open(&limit).is_err() {
            // (Root ignores file modes; the lock cannot be simulated then.)
            assert!(care.notes.iter().any(|n| n.contains("locked by firmware")), "{:?}", care.notes);
            assert_eq!(t.get(&format!("{PSTATE}/max_perf_pct")), "85", "the P-state cap still holds");
        }
    }

    #[test]
    fn hot_battery_pauses_dreams_and_caps_the_cpu() {
        let t = macbook("battery-heat");
        let mut care = act(&t, Config::default());
        let dreaming = Being { state: Some("asleep".into()), lid_closed: Some(true), dreaming: true, state_fresh: true, state_epoch: Some(1) };
        care.step(&dreaming, 0.0);
        assert!(care.dream_allowed());
        assert!(care.report().data["cpu_reasons"].to_string().contains("dreaming"));
        t.file(&format!("{SMC}/temp1_input"), "41000\n");
        care.step(&dreaming, 1.0);
        assert!(!care.dream_allowed());
        assert!(!care.report().data["cpu_reasons"].to_string().contains("dreaming"), "a paused dream is not driven");
        t.file(&format!("{SMC}/temp1_input"), "46000\n");
        care.step(&being("speaking", false), 2.0);
        assert_eq!(t.get(&format!("{PSTATE}/max_perf_pct")), "50");
        t.file(&format!("{SMC}/temp1_input"), "38000\n");
        care.step(&being("speaking", false), 3.0);
        assert!(!care.dream_allowed(), "not cool enough");
        t.file(&format!("{SMC}/temp1_input"), "36000\n");
        care.step(&being("speaking", false), 4.0);
        assert!(care.dream_allowed());
        assert_eq!((t.get(&format!("{PSTATE}/max_perf_pct")), t.get(&format!("{PSTATE}/no_turbo"))), ("100".into(), "0".into()));
    }

    #[test]
    fn a_journal_that_cannot_be_written_changes_nothing_and_says_so() {
        // As under ProtectSystem=strict with no writable runtime directory:
        // the journal's directory cannot be made (here a file is in the way).
        let t = macbook("journal-ro");
        t.file("sys/class/hwmon/hwmon3/temp1_input", "90000\n");
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        t.file("run/being", "not a directory\n");
        let mut care = act(&t, Config { charge_limit: Some(80), ..Config::default() });
        for s in 0..30 {
            care.step(&being("thinking", false), f64::from(s));
        }
        assert_eq!(t.get(&format!("{PSTATE}/no_turbo")), "0", "never changed without a journal");
        assert_eq!(t.get(&format!("{SMC}/fan1_min")), "2000");
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "100");
        let notes = care.notes.join("\n");
        assert!(notes.contains("cannot be written") && notes.contains("nothing will be changed"), "{notes}");
        let report = care.report();
        assert_eq!(report.str("mode"), Some("act"));
        assert_eq!(report.bool("acting"), Some(false), "a refused cap is not shown as held");
        let refused: Vec<_> = report.data["not_applied"].as_array().unwrap().iter().filter_map(|v| v.as_str()).collect();
        for label in ["charge_limit", "fan1_min", "no_turbo"] {
            assert!(refused.contains(&label), "{label} missing from {refused:?}");
        }
        assert!(report.str("journal_error").is_some());

        // Writable: everything is set and the report says it is held.
        let t = macbook("journal-rw");
        t.file("sys/class/hwmon/hwmon3/temp1_input", "90000\n");
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        let mut care = act(&t, Config::default());
        care.step(&being("thinking", false), 0.0);
        assert_eq!(t.get(&format!("{PSTATE}/no_turbo")), "1");
        let report = care.report();
        assert_eq!(report.bool("acting"), Some(true));
        assert_eq!(report.data["not_applied"].as_array().map(Vec::len), Some(0));
        assert!(report.data.get("journal_error").is_none());
        // Observe mode never claims to act.
        let mut watch = Care::new(&t.0, &t.journal(), Config::default(), Mode::Observe);
        watch.step(&being("thinking", false), 0.0);
        assert_eq!(watch.report().bool("acting"), Some(false));
    }

    #[test]
    fn night_work_is_held_at_the_target_without_a_dreaming_event() {
        // The night shift runs asleep with the lid closed; the mind sends
        // no `mind.dreaming`, yet the package is held at the dream target.
        let t = macbook("night");
        t.file("sys/class/hwmon/hwmon3/temp1_input", "85000\n");
        let mut care = act(&t, Config::default());
        for s in 0..20 {
            care.step(&being("asleep", true), f64::from(s));
        }
        let pct: i64 = t.get(&format!("{PSTATE}/max_perf_pct")).parse().unwrap();
        assert!(pct < 60, "hot night work is capped: {pct}");
        assert!(care.report().data["cpu_reasons"].to_string().contains("night"));
        // Cool and idle: the loop opens straight up.
        t.file("sys/class/hwmon/hwmon3/temp1_input", "40000\n");
        let mut cool = act(&t, Config::default());
        cool.step(&being("asleep", true), 0.0);
        assert_eq!(t.get(&format!("{PSTATE}/max_perf_pct")), "100");
        // Lid open while asleep: not night work.
        t.file("sys/class/hwmon/hwmon3/temp1_input", "85000\n");
        let mut open = act(&macbook("night-open"), Config::default());
        open.step(&being("asleep", false), 0.0);
        assert!(!open.report().data["cpu_reasons"].to_string().contains("night"));
    }

    #[test]
    fn the_battery_is_read_once_a_minute_not_every_pass() {
        // 25 November, live: a 1 s poll of the Smart Battery cost about 126
        // EC interrupts a second. The loop still runs every second for the
        // cheap readings.
        let t = macbook("battery-poll");
        let cfg = Config { charge_limit: Some(80), ..Config::default() };
        let mut care = Care::new(&t.0, &t.journal(), cfg, Mode::Observe);
        assert!(care.notes.iter().any(|n| n.contains("battery figures: read every 60 s on AC")), "{:?}", care.notes);
        for s in 0..=600 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads, 11, "at 0, 60, ... 600 s");
        // The fan and package still follow every second.
        t.file("sys/class/hwmon/hwmon3/temp1_input", "90000\n");
        care.step(&being("idle", false), 601.0);
        assert!(care.report().f64("package_c").unwrap() > 50.0);
        assert_eq!(care.battery_reads, 11);
        // Unplugged: read at once, once more 4 s on, then every 30 s.
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        t.file("sys/class/power_supply/BAT0/status", "Discharging\n");
        care.step(&being("idle", false), 602.0);
        assert_eq!(care.battery_reads, 12);
        assert_eq!(care.reading.status.as_deref(), Some("Discharging"));
        for s in 603..=662 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads, 14, "the follow-up at 606 s, then 636 s");
        // A change of charge is seen within the period, not before.
        t.file("sys/class/power_supply/BAT0/capacity", "15\n");
        care.step(&being("idle", false), 663.0);
        assert_eq!(care.reading.percent, Some(70.0));
        for s in 664..=692 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.reading.percent, Some(15.0));
        // Low: every 10 s.
        let before = care.battery_reads;
        for s in 693..=722 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads - before, 3);
    }

    #[test]
    fn a_status_that_lags_the_adapter_is_corrected_seconds_later() {
        // Unplugged: ADP1/online says 0 at once, but the gauge still says
        // Charging on the first read. One follow-up read 4 s on fixes it.
        let t = macbook("status-lag");
        let mut care = Care::new(&t.0, &t.journal(), Config::default(), Mode::Observe);
        t.file("sys/class/power_supply/BAT0/status", "Charging\n");
        care.step(&being("idle", false), 0.0);
        t.file("sys/class/power_supply/ADP1/online", "0\n");
        care.step(&being("idle", false), 10.0);
        assert_eq!(care.battery_reads, 2);
        assert_eq!(care.reading.status.as_deref(), Some("Charging"), "the stale edge read");
        t.file("sys/class/power_supply/BAT0/status", "Discharging\n");
        for s in 11..=13 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads, 2);
        care.step(&being("idle", false), 14.0);
        assert_eq!(care.battery_reads, 3);
        assert_eq!(care.reading.status.as_deref(), Some("Discharging"));
        // Then only the slow cadence: no second follow-up.
        for s in 15..=43 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads, 3);
    }

    #[test]
    fn a_pack_that_goes_while_bat0_stays_listed_is_seen_from_the_uevent() {
        // The sbs driver keeps BAT0 and flips present to 0; the adapter does
        // not change. The udev rule touches the stamp on the uevent.
        let t = macbook("uevent");
        let mut care = Care::new(&t.0, &t.journal(), Config::default(), Mode::Observe);
        for s in 0..=5 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads, 1);
        assert_eq!(care.reading.battery_present, Some(true));
        t.file("sys/class/power_supply/BAT0/present", "0\n");
        care.step(&being("idle", false), 6.0);
        assert_eq!(care.reading.battery_present, Some(true), "no uevent yet: not seen");
        t.file(hw::POWER_STAMP, "");
        care.step(&being("idle", false), 7.0);
        assert_eq!(care.battery_reads, 2);
        assert_eq!(care.reading.battery_present, Some(false), "seen on the uevent, not a minute later");
        assert_eq!(care.reading.percent, None);
        // The follow-up 4 s on, then quiet: one uevent is one read and one follow-up.
        for s in 8..=60 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(care.battery_reads, 3);
    }

    #[test]
    fn the_charge_cap_is_judged_on_the_share_of_full_not_of_the_design() {
        // The laptop's sbs battery: capacity is against the design (80 when
        // full on a pack at 80% health). Resting at an 80% cap it reads 64.
        let t = macbook("cap-share");
        let bat = "sys/class/power_supply/BAT0";
        t.file(&format!("{SMC}/battery_charge_limit"), "80\n");
        t.file(&format!("{bat}/status"), "Full\n");
        t.file(&format!("{bat}/capacity"), "64\n");
        t.file(&format!("{bat}/charge_now"), "3703200\n");
        t.file(&format!("{bat}/charge_full"), "4629000\n");
        t.file(&format!("{bat}/charge_full_design"), "5770000\n");
        let mut care = Care::new(&t.0, &t.journal(), Config { charge_limit: Some(80), ..Config::default() }, Mode::Observe);
        care.step(&being("idle", false), 0.0);
        assert_eq!(care.reading.percent, Some(80.0));
        assert_eq!(care.report().bool("cap_honoured"), Some(true));
    }

    #[test]
    fn a_machine_with_nothing_is_left_alone_and_says_so() {
        let t = Tree::new("bare");
        let mut care = act(&t, Config { charge_limit: Some(80), ..Config::default() });
        for s in 0..30 {
            care.step(&being("idle", false), f64::from(s));
        }
        let notes = care.notes.join("\n");
        for want in ["charge limit: no control", "fan floor: not found", "package temperature: no coretemp", "turbo switch: not found", "no control exists"] {
            assert!(notes.contains(want), "missing {want:?} in\n{notes}");
        }
        assert!(!t.journal().exists());
        assert!(care.restore().is_empty());
    }
}

//! The controller: one pass reads the hardware, decides with `policy`, and
//! (in act mode) writes only what differs, journalling each original first.
//! In observe mode it decides and reports exactly the same, and writes
//! nothing at all.

use crate::hw::{self, Hardware, Reading};
use crate::journal::{Journal, Kind};
use crate::policy::{self, Being, Config, CpuCap, DreamLoop, Latch};
use bus::Event;
use std::collections::{HashMap, HashSet};
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

pub struct Care {
    root: PathBuf,
    pub cfg: Config,
    pub mode: Mode,
    pub hw: Hardware,
    journal: Journal,
    discovered_at: f64,
    last_at: Option<f64>,
    package: Option<f64>,
    dream_pause: Latch,
    cpu_gate: Latch,
    dream: DreamLoop,
    fan_floor: Option<i64>,
    fan_stepped_at: f64,
    charge_checked_at: Option<f64>,
    over_since: Option<f64>,
    honoured: Option<bool>,
    rapl_locked: bool,
    said: HashSet<String>,
    /// Log lines waiting to be printed.
    pub notes: Vec<String>,
    /// What act mode would write (observe) or last wrote (act).
    pub wanted: HashMap<&'static str, String>,
    pub cap: Option<CpuCap>,
    pub fan_target: Option<i64>,
    pub factory_fan_min: Option<i64>,
    pub reading: Reading,
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
            dream_pause: Latch::default(),
            cpu_gate: Latch::default(),
            dream: DreamLoop::default(),
            fan_floor: None,
            fan_stepped_at: f64::NEG_INFINITY,
            charge_checked_at: None,
            over_since: None,
            honoured: None,
            rapl_locked: false,
            said: HashSet::new(),
            notes: Vec::new(),
            wanted: HashMap::new(),
            cap: None,
            fan_target: None,
            factory_fan_min: None,
            reading: Reading::default(),
        };
        for line in care.hw.survey() {
            care.notes.push(line);
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
            return false;
        }
        if self.mode == Mode::Observe {
            let shown = current.clone().unwrap_or_else(|| "?".into());
            self.once(format!("would:{label}:{value}"), || format!("observe: would set {label} from {shown} to {value}"));
            return false;
        }
        let Some(original) = current.or_else(|| hw::read_text(&path)) else {
            self.once(format!("unreadable:{label}"), || format!("{label}: unreadable, so not changed"));
            return false;
        };
        if let Err(e) = self.journal.remember(&path, kind, &original) {
            self.once(format!("journal:{label}"), || format!("{label}: could not journal the original ({e}), so not changed"));
            return false;
        }
        match hw::write_value(&path, &value) {
            Ok(()) => {
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
        let r = hw::read(&self.hw);
        let dt = self.last_at.map_or(1.0, |at| (now - at).max(0.0));
        self.last_at = Some(now);
        self.package = policy::smooth(self.package, r.package_c, dt, self.cfg.smoothing);
        if r.package_c.is_none() {
            self.package = None;
        }
        let cfg = self.cfg.clone();

        // Battery heat gates.
        let pause = self.dream_pause.update(r.battery_c, cfg.battery_pause_c, cfg.battery_resume_c);
        let hot = self.cpu_gate.update(r.battery_c, cfg.battery_cap_c, cfg.battery_resume_c);
        if pause {
            self.once("pause", || "battery over 40 C: dreams paused until it is under 37 C".into());
        } else {
            self.said.remove("pause");
        }

        // CPU caps by state.
        let dreaming = being.dreaming && !pause;
        let dream_pct = if dreaming {
            Some(self.dream.update(self.package, cfg.dream_target_c, cfg.dream_min_pct, dt))
        } else {
            self.dream.reset();
            None
        };
        let cap = policy::cpu_cap(&cfg, being, self.package, r.on_ac, r.battery_present, hot, dream_pct);
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
            }
        }
        self.cap = Some(cap);

        // Fan floor: fan1_min only, never manual.
        if let (Some(path), Some(current)) = (self.hw.fan_min.clone(), r.fan_min) {
            // After a restart the journal, not the raised value, is the factory floor.
            let factory = self.journal.original(&path).and_then(|v| v.parse().ok()).unwrap_or(current);
            self.factory_fan_min = Some(factory);
            let floor = *self.fan_floor.get_or_insert(current);
            let target = policy::fan_target(&cfg, self.package, factory, r.fan_max);
            self.fan_target = Some(target);
            if now - self.fan_stepped_at >= cfg.fan_step_every {
                let next = policy::fan_next(&cfg, floor, target, being.listening());
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
            // Is it honoured? On AC, well over the limit and still charging.
            let over = r.on_ac == Some(true)
                && r.percent.is_some_and(|p| p >= limit as f64 + cfg.honour_margin)
                && r.status.as_deref() == Some("Charging");
            if over {
                let since = *self.over_since.get_or_insert(now);
                if now - since >= cfg.honour_after && self.honoured != Some(false) {
                    self.honoured = Some(false);
                    self.notes.push(format!("the charge limit of {limit}% is not being honoured: still charging at {:?}%", r.percent));
                }
            } else {
                self.over_since = None;
                let resting = matches!(r.status.as_deref(), Some("Not charging" | "Full"));
                if r.on_ac == Some(true) && resting && r.percent.is_some_and(|p| p >= limit as f64 - cfg.honour_margin) {
                    self.honoured = Some(true);
                }
            }
        }
        self.reading = r;
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
            .with("fan_rpm", r.fan_rpm)
            .with("fan_floor", self.fan_floor)
            .with("fan_target", self.fan_target)
            .with("fan_factory_min", self.factory_fan_min);
        if let Some(limit) = self.cfg.charge_limit {
            ev = ev
                .with("charge_limit", limit)
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
        t.file(&format!("{RAPL}/name"), "package-0\n");
        t.file(&format!("{RAPL}/constraint_0_name"), "long_term\n");
        t.file(&format!("{RAPL}/constraint_0_power_limit_uw"), "35000000\n");
        t.file(&format!("{RAPL}/constraint_1_name"), "short_term\n");
        t.file(&format!("{RAPL}/constraint_1_power_limit_uw"), "43750000\n");
        t.file("sys/class/powercap/intel-rapl:0:0/name", "core\n");
        t
    }

    fn being(state: &str, lid_closed: bool) -> Being {
        Being { state: Some(state.into()), lid_closed: Some(lid_closed), dreaming: false }
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
        // The owner is talking: held steady.
        for s in 11..60 {
            care.step(&being("listening", false), f64::from(s));
        }
        assert_eq!(floor(&t), 2500);
        // The lid closes: raised again even in an attending state.
        for s in 60..400 {
            care.step(&being("attending", true), f64::from(s));
        }
        assert_eq!(floor(&t), 5250);
        assert_eq!(t.get(&format!("{SMC}/fan1_manual")), "0", "never manual mode");
        // Cool again: back down by steps to the factory floor.
        t.file("sys/class/hwmon/hwmon3/temp1_input", "45000\n");
        for s in 400..900 {
            care.step(&being("idle", false), f64::from(s));
        }
        assert_eq!(floor(&t), 2000);
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
        // No limit chosen: never touched.
        let t = macbook("charge-none");
        let mut care = act(&t, Config::default());
        care.step(&being("idle", false), 0.0);
        assert_eq!(t.get(&format!("{SMC}/battery_charge_limit")), "100");
        assert!(care.report().data.get("charge_limit").is_none());
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
        let dreaming = Being { state: Some("asleep".into()), lid_closed: Some(true), dreaming: true };
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

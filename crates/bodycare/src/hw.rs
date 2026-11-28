//! The power and thermal controls Linux shows in sysfs, found under a root
//! (`/` on the laptop, a fixture tree in tests). Every control is optional:
//! what is absent is reported once and left alone.
//!
//! On the MacBookPro9,2 with Linux 6.18:
//! - charge limit: `power_supply/*/charge_control_end_threshold` (a kernel
//!   standard some drivers offer) or the in-tree applesmc patch's
//!   `applesmc.768/battery_charge_limit` (BCLM, task 2.7);
//! - fan floor: `applesmc.768/fan1_min` (and its `fan1_max`, `fan1_input`).
//!   `fan1_manual` is never written: if this program dies, the SMC's own
//!   curve is still in charge;
//! - package temperature: coretemp's channel labelled "Package id 0";
//! - battery temperature: applesmc's TB0T-TB2T, or the battery's `temp`
//!   only when the SMC has no battery sensor;
//! - battery charge: `charge_now` over `charge_full` (or the energy pair),
//!   because this Mac's ACPI Smart Battery driver (`sbs`) reports
//!   `capacity` against the design capacity (AbsoluteStateOfCharge): a full
//!   pack that has worn to 80% of new reads "80";
//! - CPU caps: intel_pstate `no_turbo` and `max_perf_pct` (present in
//!   passive mode too), and RAPL package PL1 when firmware has not locked it;
//! - CPU load: the first line of `/proc/stat`, so the fan floor follows
//!   sustained work rather than an idle package that runs warm.
//!
//! The battery's own files are expensive here. Every attribute of the `sbs`
//! battery (present, status, capacity, charge_*, temp) refreshes through
//! the embedded controller once its 1 s cache has expired: on the laptop one
//! refresh measured about 126 EC interrupts (GPE 0x17) and 55-64 ms blocked.
//! So `read` is split: `read_battery` (slow, called every 30-60 s by the
//! controller) and `read_fast` (adapter, SMC, coretemp, fan and CPU
//! controls, which cost no EC traffic, every pass).
//!
//! A pack that goes away while `BAT0` stays listed (the `sbs` driver keeps
//! the directory and flips `present` to 0) is not visible to the cheap
//! reads. The driver sends a power_supply change uevent then, as it does for
//! an adapter change; 60-being-power.rules touches `POWER_STAMP` on every
//! such uevent, and its modification time (a stat, no EC traffic) is
//! checked every pass, so the battery is read within `battery_poke_gap`.

use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

/// Touched by 60-being-power.rules on every power_supply change uevent,
/// relative to the root.
pub const POWER_STAMP: &str = "run/being-power-changed";

/// When the power supplies last sent a change uevent, as the stamp's
/// modification time; None when no uevent has come since boot.
pub fn power_stamp(root: &Path) -> Option<SystemTime> {
    fs::metadata(root.join(POWER_STAMP)).and_then(|m| m.modified()).ok()
}

const MAX_VALUE: u64 = 4096;
const MAX_ENTRIES: usize = 256;

pub fn read_text(path: &Path) -> Option<String> {
    let mut data = String::new();
    fs::File::open(path).ok()?.take(MAX_VALUE).read_to_string(&mut data).ok()?;
    let data = data.trim();
    (!data.is_empty()).then(|| data.to_owned())
}

pub fn read_i64(path: &Path) -> Option<i64> {
    read_text(path)?.parse().ok()
}

/// Write one value to a sysfs attribute, as `echo value > path` would.
pub fn write_value(path: &Path, value: &str) -> io::Result<()> {
    let mut file = fs::OpenOptions::new().write(true).truncate(true).open(path)?;
    file.write_all(format!("{value}\n").as_bytes())
}

fn list(dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else { return Vec::new() };
    let mut entries: Vec<_> = entries.flatten().take(MAX_ENTRIES).map(|e| e.path()).collect();
    entries.sort();
    entries
}

fn name_of(path: &Path) -> String {
    path.file_name().map_or_else(String::new, |n| n.to_string_lossy().into_owned())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChargeKind {
    /// The kernel's standard `charge_control_end_threshold`.
    EndThreshold,
    /// The applesmc patch's BCLM attribute.
    Applesmc,
}

impl ChargeKind {
    pub fn name(self) -> &'static str {
        match self {
            ChargeKind::EndThreshold => "charge_control_end_threshold",
            ChargeKind::Applesmc => "applesmc battery_charge_limit",
        }
    }
}

/// Where everything is. Found once at start and again now and then, since
/// drivers can appear late.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Hardware {
    pub battery: Option<PathBuf>,
    pub adapters: Vec<PathBuf>,
    pub charge: Option<(PathBuf, ChargeKind)>,
    pub smc: Option<PathBuf>,
    /// fan1_min, fan1_max and fan1_input.
    pub fan_min: Option<PathBuf>,
    pub fan_max: Option<PathBuf>,
    pub fan_input: Option<PathBuf>,
    /// The "Package id 0" channel, or the coretemp chip's channels.
    pub package: Vec<PathBuf>,
    pub package_labelled: bool,
    /// Battery temperature inputs, in millidegrees (SMC) ...
    pub battery_temps: Vec<PathBuf>,
    /// ... and the battery's own, in tenths of a degree.
    pub battery_temp_tenths: Option<PathBuf>,
    pub no_turbo: Option<PathBuf>,
    pub min_perf: Option<PathBuf>,
    pub max_perf: Option<PathBuf>,
    /// RAPL package zone's long-term (PL1) limit, microwatts.
    pub rapl_pl1: Option<PathBuf>,
    /// `/proc/stat`, for the CPU's busy share.
    pub stat: Option<PathBuf>,
}

fn find_smc(root: &Path) -> Option<PathBuf> {
    let direct = root.join("sys/devices/platform/applesmc.768");
    if direct.is_dir() {
        return Some(direct);
    }
    list(&root.join("sys/bus/platform/devices"))
        .into_iter()
        .find(|p| name_of(p).starts_with("applesmc") && p.is_dir())
}

fn temp_channels(dir: &Path) -> Vec<(String, PathBuf)> {
    list(dir)
        .into_iter()
        .filter_map(|path| {
            let name = name_of(&path);
            let n = name.strip_prefix("temp")?.strip_suffix("_input")?;
            (!n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())).then(|| (n.to_owned(), path))
        })
        .collect()
}

impl Hardware {
    pub fn discover(root: &Path) -> Hardware {
        let mut hw = Hardware::default();
        for dev in list(&root.join("sys/class/power_supply")) {
            if read_text(&dev.join("scope")).as_deref() == Some("Device") {
                continue; // a mouse's battery is not the body's
            }
            match read_text(&dev.join("type")).as_deref() {
                Some("Battery") if hw.battery.is_none() => {
                    let threshold = dev.join("charge_control_end_threshold");
                    if threshold.is_file() {
                        hw.charge = Some((threshold, ChargeKind::EndThreshold));
                    }
                    if dev.join("temp").is_file() {
                        hw.battery_temp_tenths = Some(dev.join("temp"));
                    }
                    hw.battery = Some(dev);
                }
                Some("Mains") => hw.adapters.push(dev),
                _ => {}
            }
        }
        hw.smc = find_smc(root);
        if let Some(smc) = &hw.smc {
            let limit = smc.join("battery_charge_limit");
            if hw.charge.is_none() && limit.is_file() {
                hw.charge = Some((limit, ChargeKind::Applesmc));
            }
            let file = |name: &str| Some(smc.join(name)).filter(|p| p.is_file());
            hw.fan_min = file("fan1_min");
            hw.fan_max = file("fan1_max");
            hw.fan_input = file("fan1_input");
            for (n, input) in temp_channels(smc) {
                let label = read_text(&smc.join(format!("temp{n}_label"))).unwrap_or_default();
                if label.starts_with("TB") && label.ends_with('T') {
                    hw.battery_temps.push(input);
                }
            }
        }
        for chip in list(&root.join("sys/class/hwmon")) {
            let name = read_text(&chip.join("name")).or_else(|| read_text(&chip.join("device/name")));
            if name.as_deref() != Some("coretemp") {
                continue;
            }
            let mut dir = chip.clone();
            let mut channels = temp_channels(&dir);
            if channels.is_empty() {
                dir = chip.join("device");
                channels = temp_channels(&dir);
            }
            let labelled = channels.iter().find(|(n, _)| {
                read_text(&dir.join(format!("temp{n}_label"))).is_some_and(|l| l == "Package id 0")
            });
            match labelled {
                Some((_, input)) => {
                    hw.package = vec![input.clone()];
                    hw.package_labelled = true;
                }
                None if hw.package.is_empty() => hw.package = channels.into_iter().map(|(_, p)| p).collect(),
                None => {}
            }
            if hw.package_labelled {
                break;
            }
        }
        hw.stat = Some(root.join("proc/stat")).filter(|p| p.is_file());
        let pstate = root.join("sys/devices/system/cpu/intel_pstate");
        hw.no_turbo = Some(pstate.join("no_turbo")).filter(|p| p.is_file());
        hw.min_perf = Some(pstate.join("min_perf_pct")).filter(|p| p.is_file());
        hw.max_perf = Some(pstate.join("max_perf_pct")).filter(|p| p.is_file());
        let powercap = root.join("sys/class/powercap");
        let zones = list(&powercap).into_iter().chain(list(&powercap.join("intel-rapl")));
        for zone in zones {
            let name = name_of(&zone);
            // intel-rapl:0 is package 0; intel-rapl:0:N are its subzones.
            if !name.starts_with("intel-rapl:") || name.matches(':').count() != 1 {
                continue;
            }
            if read_text(&zone.join("name")).as_deref() != Some("package-0") {
                continue;
            }
            for c in 0..4 {
                if read_text(&zone.join(format!("constraint_{c}_name"))).as_deref() == Some("long_term") {
                    let limit = zone.join(format!("constraint_{c}_power_limit_uw"));
                    if limit.is_file() {
                        hw.rapl_pl1 = Some(limit);
                    }
                }
            }
            if hw.rapl_pl1.is_some() {
                break;
            }
        }
        hw
    }

    /// One line per control: what was found, and what was not.
    pub fn survey(&self) -> Vec<String> {
        let found = |what: &str, p: &Option<PathBuf>| match p {
            Some(p) => format!("{what}: {}", p.display()),
            None => format!("{what}: not found; left alone"),
        };
        vec![
            match &self.charge {
                Some((p, kind)) => format!("charge limit: {} ({})", p.display(), kind.name()),
                None => "charge limit: no control (needs the applesmc patch, task 2.7); left alone".into(),
            },
            found("fan floor", &self.fan_min),
            if self.package.is_empty() {
                "package temperature: no coretemp; fan and heat caps follow the battery and state only".into()
            } else if self.package_labelled {
                format!("package temperature: {}", self.package[0].display())
            } else {
                format!("package temperature: hottest of {} coretemp channels (no \"Package id 0\")", self.package.len())
            },
            if self.battery_temps.is_empty() && self.battery_temp_tenths.is_none() {
                "battery temperature: not found; the battery heat gate is off".into()
            } else {
                format!("battery temperature: {} sensor(s)", self.battery_temps.len() + usize::from(self.battery_temp_tenths.is_some()))
            },
            found("turbo switch", &self.no_turbo),
            found("minimum performance", &self.min_perf),
            found("performance cap", &self.max_perf),
            found("RAPL package PL1", &self.rapl_pl1),
            match &self.stat {
                Some(p) => format!("CPU load: {}", p.display()),
                None => "CPU load: no /proc/stat; the fan floor follows the temperature alone".into(),
            },
        ]
    }
}

/// What the hardware says right now.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Reading {
    pub package_c: Option<f64>,
    pub battery_c: Option<f64>,
    pub on_ac: Option<bool>,
    pub battery_present: Option<bool>,
    pub percent: Option<f64>,
    pub status: Option<String>,
    pub fan_rpm: Option<i64>,
    pub fan_min: Option<i64>,
    pub fan_max: Option<i64>,
    pub charge_limit: Option<i64>,
    pub no_turbo: Option<bool>,
    pub min_perf: Option<i64>,
    pub max_perf: Option<i64>,
    pub rapl_pl1_uw: Option<i64>,
    /// All CPUs' (busy, total) ticks since boot, from `/proc/stat`.
    pub cpu_ticks: Option<(u64, u64)>,
}

/// The first line of `/proc/stat` as (busy, total) ticks: busy is every
/// field but idle and iowait (nice counts: the model runs niced, and it is
/// heat all the same). Guest time is already inside user and nice, so only
/// the first eight fields are summed.
pub fn cpu_ticks(text: &str) -> Option<(u64, u64)> {
    let line = text.lines().next()?;
    let mut fields = line.split_ascii_whitespace();
    if fields.next()? != "cpu" {
        return None;
    }
    let values: Vec<u64> = fields.take(8).map(|f| f.parse().ok()).collect::<Option<_>>()?;
    if values.len() < 5 {
        return None;
    }
    let total = values.iter().try_fold(0u64, |sum, v| sum.checked_add(*v))?;
    let busy = total - values[3] - values[4];
    Some((busy, total))
}

/// What the battery itself says: the slow half of a reading.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct BatteryReading {
    /// The battery directory these figures came from.
    pub dir: Option<PathBuf>,
    /// The `present` attribute said "0".
    pub absent: bool,
    pub percent: Option<f64>,
    pub status: Option<String>,
    /// The battery's own temperature, only read when the SMC has none.
    pub temp_c: Option<f64>,
}

/// The charge as the owner means it: how full the pack is now against what
/// it holds when full today, as a whole percent.
///
/// `charge_now / charge_full` (or the energy pair) comes first: the ACPI
/// Smart Battery driver on the MacBookPro9,2 reports `capacity` as the
/// pack's AbsoluteStateOfCharge, against the design capacity. Measured on
/// the laptop: status Full, capacity 80, charge_now 4593000, charge_full
/// 4629000, charge_full_design 5770000 (99.2% of full, 79.6% of design).
/// `capacity` is the fallback for batteries without the pairs (on most
/// other drivers it is the same ratio). A pack that reports Full and is
/// within 3 points of full is shown as 100: the gauge stopped the charge.
pub fn charge_percent(dir: &Path) -> Option<f64> {
    let status = read_text(&dir.join("status"));
    let ratio = ["charge", "energy"].into_iter().find_map(|kind| {
        let now = read_i64(&dir.join(format!("{kind}_now"))).filter(|v| *v >= 0)?;
        let full = read_i64(&dir.join(format!("{kind}_full"))).filter(|v| *v > 0)?;
        Some((now as f64 * 100.0 / full as f64).clamp(0.0, 100.0))
    });
    let percent = match ratio {
        Some(r) if status.as_deref() == Some("Full") && r >= 97.0 => 100.0,
        Some(r) => r.round(),
        None => read_i64(&dir.join("capacity")).filter(|p| (0..=100).contains(p))? as f64,
    };
    Some(percent)
}

/// Read the battery's own files (the expensive part; see the module notes).
pub fn read_battery(hw: &Hardware) -> BatteryReading {
    let Some(dir) = hw.battery.as_ref().filter(|b| b.is_dir()) else {
        return BatteryReading { dir: hw.battery.clone(), ..BatteryReading::default() };
    };
    if read_text(&dir.join("present")).as_deref() == Some("0") {
        return BatteryReading { dir: Some(dir.clone()), absent: true, ..BatteryReading::default() };
    }
    let temp_c = if hw.battery_temps.is_empty() {
        hw.battery_temp_tenths.as_ref().and_then(|p| read_i64(p)).filter(|t| (1..900).contains(t)).map(|t| t as f64 / 10.0)
    } else {
        None
    };
    BatteryReading {
        dir: Some(dir.clone()),
        absent: false,
        percent: charge_percent(dir),
        status: read_text(&dir.join("status")),
        temp_c,
    }
}

/// On mains power? From the adapters' `online`, which the drivers answer
/// from their own state (no EC traffic on the laptop). One adapter saying
/// "off" is not proof while another cannot be read.
pub fn read_ac(hw: &Hardware) -> Option<bool> {
    let mut on_ac = None;
    let mut unreadable = false;
    for adapter in &hw.adapters {
        match read_text(&adapter.join("online")).as_deref() {
            Some("1") => on_ac = Some(true),
            Some("0") if on_ac.is_none() => on_ac = Some(false),
            Some("0") => {}
            _ => unreadable = true,
        }
    }
    if unreadable && on_ac == Some(false) {
        on_ac = None;
    }
    on_ac
}

/// Everything at once, battery included (one-off passes and tests).
pub fn read(hw: &Hardware) -> Reading {
    read_fast(hw, &read_battery(hw))
}

/// The cheap reads, with the battery's figures from the last `read_battery`.
pub fn read_fast(hw: &Hardware, battery: &BatteryReading) -> Reading {
    let milli = |p: &PathBuf| read_i64(p).filter(|v| (1_000..=150_000).contains(v)).map(|v| v as f64 / 1000.0);
    // One unreadable package channel means the hottest may be the missing one.
    let package: Option<Vec<f64>> = hw.package.iter().map(milli).collect();
    let package_c = package.and_then(|v| v.into_iter().reduce(f64::max));
    // Battery sensors: SMC keys are often junk, so a bad one is skipped. The
    // battery's own `temp` stands in only where the SMC has no battery key.
    let battery_c = hw.battery_temps.iter().filter_map(milli).filter(|c| *c < 90.0).reduce(f64::max).or(battery.temp_c);
    let on_ac = read_ac(hw);
    let dir = hw.battery.as_ref();
    // A pack pulled out may take its whole directory with it before the next
    // discovery: a battery that has gone is not present. (A directory check
    // costs nothing; the `present` file is part of the slow read.)
    let here = dir.is_some_and(|b| b.is_dir());
    let figures = here && battery.dir.as_ref() == dir;
    let battery_present = dir.map(|_| here && !(figures && battery.absent));
    let figures = figures && !battery.absent;
    Reading {
        package_c,
        battery_c,
        on_ac,
        battery_present: battery_present.or(Some(false)).filter(|_| dir.is_some() || !hw.adapters.is_empty()),
        percent: battery.percent.filter(|_| figures),
        status: battery.status.clone().filter(|_| figures),
        fan_rpm: hw.fan_input.as_ref().and_then(|p| read_i64(p)),
        fan_min: hw.fan_min.as_ref().and_then(|p| read_i64(p)).filter(|v| (0..=10_000).contains(v)),
        fan_max: hw.fan_max.as_ref().and_then(|p| read_i64(p)).filter(|v| (1..=10_000).contains(v)),
        charge_limit: hw.charge.as_ref().and_then(|(p, _)| read_i64(p)),
        no_turbo: hw.no_turbo.as_ref().and_then(|p| read_text(p)).map(|v| v == "1"),
        min_perf: hw.min_perf.as_ref().and_then(|p| read_i64(p)).filter(|v| (1..=100).contains(v)),
        max_perf: hw.max_perf.as_ref().and_then(|p| read_i64(p)),
        rapl_pl1_uw: hw.rapl_pl1.as_ref().and_then(|p| read_i64(p)),
        cpu_ticks: hw.stat.as_ref().and_then(|p| read_text(p)).and_then(|t| cpu_ticks(&t)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Tree(PathBuf);

    impl Tree {
        fn new(tag: &str) -> Tree {
            let root = std::env::temp_dir().join(format!("bodycare-hw-{}-{tag}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).unwrap();
            Tree(root)
        }

        fn file(&self, path: &str, data: &str) {
            let path = self.0.join(path);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, data).unwrap();
        }
    }

    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const BAT: &str = "sys/class/power_supply/BAT0";

    #[test]
    fn a_full_worn_pack_reads_full_not_its_share_of_the_design() {
        // The laptop on 25 November: sbs reports capacity against design.
        let t = Tree::new("full");
        for (name, value) in [
            ("type", "Battery"),
            ("status", "Full"),
            ("capacity", "80"),
            ("charge_now", "4593000"),
            ("charge_full", "4629000"),
            ("charge_full_design", "5770000"),
        ] {
            t.file(&format!("{BAT}/{name}"), &format!("{value}\n"));
        }
        let dir = t.0.join(BAT);
        assert_eq!(charge_percent(&dir), Some(100.0), "full is full");
        // Charging at the same charge: the honest 99%.
        t.file(&format!("{BAT}/status"), "Charging\n");
        assert_eq!(charge_percent(&dir), Some(99.0));
        // Discharging: a share of today's full, not of the design.
        t.file(&format!("{BAT}/status"), "Discharging\n");
        t.file(&format!("{BAT}/charge_now"), "2314500\n");
        t.file(&format!("{BAT}/capacity"), "40\n");
        assert_eq!(charge_percent(&dir), Some(50.0));
        // A charge cap holding at 80% is 80%, Full or not.
        t.file(&format!("{BAT}/status"), "Full\n");
        t.file(&format!("{BAT}/charge_now"), "3703200\n");
        assert_eq!(charge_percent(&dir), Some(80.0));
        // Without the pairs, capacity is all there is.
        fs::remove_file(dir.join("charge_now")).unwrap();
        assert_eq!(charge_percent(&dir), Some(40.0));
    }

    #[test]
    fn energy_figures_and_nonsense() {
        let t = Tree::new("energy");
        t.file(&format!("{BAT}/energy_now"), "30000000\n");
        t.file(&format!("{BAT}/energy_full"), "60000000\n");
        t.file(&format!("{BAT}/capacity"), "41\n");
        let dir = t.0.join(BAT);
        assert_eq!(charge_percent(&dir), Some(50.0));
        t.file(&format!("{BAT}/energy_now"), "70000000\n");
        assert_eq!(charge_percent(&dir), Some(100.0), "more than full clamps");
        t.file(&format!("{BAT}/energy_full"), "0\n");
        assert_eq!(charge_percent(&dir), Some(41.0), "a zero full falls back to capacity");
        t.file(&format!("{BAT}/capacity"), "250\n");
        assert_eq!(charge_percent(&dir), None);
    }

    #[test]
    fn the_battery_temp_is_read_only_when_the_smc_has_no_battery_sensor() {
        let t = Tree::new("temp");
        t.file(&format!("{BAT}/type"), "Battery\n");
        t.file(&format!("{BAT}/temp"), "450\n");
        t.file("sys/devices/platform/applesmc.768/temp1_label", "TB0T\n");
        t.file("sys/devices/platform/applesmc.768/temp1_input", "31000\n");
        let hw = Hardware::discover(&t.0);
        assert_eq!(read_battery(&hw).temp_c, None, "the SMC key is enough; no EC read");
        assert_eq!(read(&hw).battery_c, Some(31.0));
        fs::remove_dir_all(t.0.join("sys/devices/platform")).unwrap();
        let hw = Hardware::discover(&t.0);
        assert_eq!(read(&hw).battery_c, Some(45.0));
    }

    #[test]
    fn cpu_ticks_come_from_the_first_line_of_proc_stat() {
        // The laptop's kernel: ten fields; guest and guest_nice are inside user.
        assert_eq!(cpu_ticks("cpu  100 20 30 800 50 0 5 0 7 0\ncpu0 1 2 3 4 5 6 7 8 9 10\n"), Some((155, 1005)));
        assert_eq!(cpu_ticks("cpu 1 2 3 4 5\n"), Some((6, 15)), "an old kernel's five fields");
        assert_eq!(cpu_ticks("cpu0 1 2 3 4 5 6 7 8\n"), None, "the per-CPU line is not the total");
        assert_eq!(cpu_ticks("cpu 1 2 x 4 5\n"), None);
        assert_eq!(cpu_ticks("cpu 1 2 3\n"), None);
        assert_eq!(cpu_ticks(""), None);
        let t = Tree::new("stat");
        t.file("proc/stat", "cpu 10 0 10 70 10 0 0 0 0 0\n");
        let hw = Hardware::discover(&t.0);
        assert!(hw.stat.is_some());
        assert_eq!(read(&hw).cpu_ticks, Some((20, 100)));
        assert_eq!(read(&Hardware::default()).cpu_ticks, None);
    }

    #[test]
    fn figures_from_another_or_a_vanished_battery_are_not_used() {
        let t = Tree::new("stale");
        t.file(&format!("{BAT}/type"), "Battery\n");
        t.file(&format!("{BAT}/capacity"), "55\n");
        t.file(&format!("{BAT}/status"), "Discharging\n");
        let hw = Hardware::discover(&t.0);
        let figures = read_battery(&hw);
        assert_eq!(read_fast(&hw, &figures).percent, Some(55.0));
        assert_eq!(read_fast(&hw, &BatteryReading::default()).percent, None, "no figures yet");
        fs::remove_dir_all(t.0.join(BAT)).unwrap();
        let r = read_fast(&hw, &figures);
        assert_eq!((r.battery_present, r.percent, r.status), (Some(false), None, None));
        // A battery that says it is not present.
        t.file(&format!("{BAT}/type"), "Battery\n");
        t.file(&format!("{BAT}/present"), "0\n");
        let r = read(&hw);
        assert_eq!((r.battery_present, r.percent), (Some(false), None));
    }
}

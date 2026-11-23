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
//! - battery temperature: applesmc's TB0T-TB2T, or the battery's `temp`;
//! - CPU caps: intel_pstate `no_turbo` and `max_perf_pct` (present in
//!   passive mode too), and RAPL package PL1 when firmware has not locked it.

use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

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
    pub max_perf: Option<PathBuf>,
    /// RAPL package zone's long-term (PL1) limit, microwatts.
    pub rapl_pl1: Option<PathBuf>,
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
        let pstate = root.join("sys/devices/system/cpu/intel_pstate");
        hw.no_turbo = Some(pstate.join("no_turbo")).filter(|p| p.is_file());
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
            found("performance cap", &self.max_perf),
            found("RAPL package PL1", &self.rapl_pl1),
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
    pub max_perf: Option<i64>,
    pub rapl_pl1_uw: Option<i64>,
}

pub fn read(hw: &Hardware) -> Reading {
    let milli = |p: &PathBuf| read_i64(p).filter(|v| (1_000..=150_000).contains(v)).map(|v| v as f64 / 1000.0);
    // One unreadable package channel means the hottest may be the missing one.
    let package: Option<Vec<f64>> = hw.package.iter().map(milli).collect();
    let package_c = package.and_then(|v| v.into_iter().reduce(f64::max));
    // Battery sensors: SMC keys are often junk, so a bad one is skipped.
    let mut battery_c = hw.battery_temps.iter().filter_map(milli).filter(|c| *c < 90.0).reduce(f64::max);
    if let Some(t) = hw.battery_temp_tenths.as_ref().and_then(|p| read_i64(p)).filter(|t| (1..900).contains(t)) {
        battery_c = Some(battery_c.map_or(t as f64 / 10.0, |c| c.max(t as f64 / 10.0)));
    }
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
    let battery = hw.battery.as_ref();
    // A pack pulled out may take its whole directory with it before the next
    // discovery: a battery that has gone is not present.
    let battery_present = battery.map(|b| b.is_dir() && read_text(&b.join("present")).as_deref() != Some("0"));
    Reading {
        package_c,
        battery_c,
        on_ac,
        battery_present: battery_present.or(Some(false)).filter(|_| battery.is_some() || !hw.adapters.is_empty()),
        percent: battery.and_then(|b| read_i64(&b.join("capacity"))).filter(|p| (0..=100).contains(p)).map(|p| p as f64),
        status: battery.and_then(|b| read_text(&b.join("status"))),
        fan_rpm: hw.fan_input.as_ref().and_then(|p| read_i64(p)),
        fan_min: hw.fan_min.as_ref().and_then(|p| read_i64(p)).filter(|v| (0..=10_000).contains(v)),
        fan_max: hw.fan_max.as_ref().and_then(|p| read_i64(p)).filter(|v| (1..=10_000).contains(v)),
        charge_limit: hw.charge.as_ref().and_then(|(p, _)| read_i64(p)),
        no_turbo: hw.no_turbo.as_ref().and_then(|p| read_text(p)).map(|v| v == "1"),
        max_perf: hw.max_perf.as_ref().and_then(|p| read_i64(p)),
        rapl_pl1_uw: hw.rapl_pl1.as_ref().and_then(|p| read_i64(p)),
    }
}

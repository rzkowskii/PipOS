//! Kernel counters sampled with the rest of the body, off the event loop.

use std::fs;
use std::path::Path;
use std::time::{Duration, Instant};

const SETTLE: Duration = Duration::from_secs(30);
const MAX_SAMPLE_GAP: Duration = Duration::from_secs(6);
// Initial audition thresholds. LAPTOP measurements must calibrate both.
const STRAIN_PCT: f64 = 5.0;
const EFFORT_START_W: f64 = 5.0;
const EFFORT_FULL_W: f64 = 20.0;
// The bit positions are also used by Doctor.
const REFLEX: [&str; 4] = ["face", "voice", "brainstem", "mind"];

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Reading {
    pub strain_pct: Option<f64>,
    pub package_w: Option<f64>,
    pub effort: Option<f64>,
    pub thermal_throttles: Option<u64>,
    pub oom_organs: Option<u8>,
}

#[derive(Default)]
struct Sustained {
    since: Option<Instant>,
    last: Option<Instant>,
}

impl Sustained {
    fn high(&mut self, now: Instant, high: bool) -> bool {
        if self.last.is_some_and(|last| now.saturating_duration_since(last) > MAX_SAMPLE_GAP) || !high {
            self.since = None;
        }
        self.last = Some(now);
        high && now.saturating_duration_since(*self.since.get_or_insert(now)) >= SETTLE
    }
}

#[derive(Default)]
pub struct Counters {
    strain: Sustained,
    effort: Sustained,
    energy: Option<(usize, u64, u64, Instant)>,
    oom_organs: u8,
}

fn number(path: &Path) -> Option<u64> {
    fs::read_to_string(path).ok()?.trim().parse().ok()
}

fn cpu_pressure(path: &Path) -> Option<f64> {
    let text = fs::read_to_string(path).ok()?;
    let mut some = text.lines().find_map(|line| line.strip_prefix("some "))?.split_ascii_whitespace();
    let pct: f64 = some.find_map(|field| field.strip_prefix("avg10="))?.parse().ok()?;
    (0.0..=100.0).contains(&pct).then_some(pct)
}

fn oom_kills(path: &Path) -> Option<u64> {
    let text = fs::read_to_string(path).ok()?;
    text.lines().find_map(|line| line.strip_prefix("oom_kill "))?.parse().ok()
}

impl Counters {
    pub fn read(&mut self, root: &Path, now: Instant) -> Reading {
        let mut pressure: Option<f64> = None;
        let mut oom_available = false;
        for (index, organ) in REFLEX.iter().enumerate() {
            let unit = root.join(format!("sys/fs/cgroup/system.slice/being-{organ}.service"));
            if let Some(pct) = cpu_pressure(&unit.join("cpu.pressure")) {
                pressure = Some(pressure.map_or(pct, |peak| peak.max(pct)));
            }
            if let Some(kills) = oom_kills(&unit.join("memory.events")) {
                oom_available = true;
                if kills > 0 {
                    self.oom_organs |= 1 << index;
                }
            }
        }
        let strained = self.strain.high(now, pressure.is_some_and(|pct| pct >= STRAIN_PCT));
        let package_w = self.package_w(root, now);
        let exerting = self.effort.high(now, package_w.is_some_and(|watts| watts >= EFFORT_START_W));
        Reading {
            strain_pct: pressure.map(|pct| if strained { pct } else { 0.0 }),
            package_w,
            effort: package_w.map(|watts| if exerting {
                ((watts - EFFORT_START_W) / (EFFORT_FULL_W - EFFORT_START_W)).clamp(0.0, 1.0)
            } else {
                0.0
            }),
            thermal_throttles: number(&root.join("sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")),
            oom_organs: (oom_available || self.oom_organs != 0).then_some(self.oom_organs),
        }
    }

    fn package_w(&mut self, root: &Path, now: Instant) -> Option<f64> {
        let sample = ["sys/class/powercap/intel-rapl:0", "sys/class/powercap/intel-rapl/intel-rapl:0"]
            .iter().enumerate().find_map(|(index, relative)| {
                let zone = root.join(relative);
                let range = number(&zone.join("max_energy_range_uj"))?;
                let value = number(&zone.join("energy_uj"))?;
                (range > 0 && value <= range).then_some((index, range, value, now))
            });
        let previous = std::mem::replace(&mut self.energy, sample);
        let (source, range, value, _) = sample?;
        let (last_source, last_range, last, at) = previous?;
        let span = now.saturating_duration_since(at);
        if source != last_source || range != last_range || span.is_zero() || span > MAX_SAMPLE_GAP {
            return None;
        }
        let delta = value.checked_sub(last).or_else(|| range.checked_sub(last)?.checked_add(value))?;
        let watts = delta as f64 / 1_000_000.0 / span.as_secs_f64();
        (0.0..=150.0).contains(&watts).then_some(watts)
    }
}

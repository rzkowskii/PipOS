//! The body's readings and the slow drives built on them.
//!
//! Simulated readings start with lab defaults. Hardware readings stay unknown
//! until the relevant device reports a value.

use crate::battery::{self, Battery};
use crate::body_work;
use bus::Event;

/// Publish at least this often, and at once when a reading changes.
const PERIOD: f64 = 5.0;

pub struct Body {
    battery: Option<f64>,
    charging: Option<bool>,
    on_ac: Option<bool>,
    /// Energy left (Wh), draw (W) and pack voltage (V), when the battery says.
    energy_wh: Option<f64>,
    power_w: Option<f64>,
    voltage_v: Option<f64>,
    /// How much the battery holds now against when new (percent), and its
    /// charge cycles, when the battery says.
    health_pct: Option<f64>,
    cycles: Option<i64>,
    temp: Option<f64>,
    lux: Option<f64>,
    work: body_work::Reading,
    lid_closed: Option<bool>,
    lid_seq: u64,
    /// The battery's keeper: pleas, time to empty and the clean power-off.
    pub care: Battery,
    /// The mind has said it has saved, so a pending power-off need not wait.
    mind_saved: bool,
    power_off_due: bool,
    recover_panel: bool,
    panel_ready: bool,
    /// A pretend hour of the day, for testing night behaviour.
    hour: Option<u32>,
    /// The owner's time zone as seconds east of UTC, told by the mind. The
    /// image itself keeps UTC, so without this "night" would be UTC's night.
    utc_offset: Option<i64>,
    clock_trusted: bool,
    /// True words about its own life (how long awake, how old), added to the
    /// senses line. Set by the brainstem from the life record.
    pub note: String,
    dirty: bool,
    last_pub: f64,
    last_contact: f64,
    last_stimulus: f64,
}

/// The temperature scale the being feels, from the measured idle of the
/// laptop (package 71-82 C, 25 November 2026) up to bodycare's `hot_package_c`
/// (92 C, where the CPU is capped). Normal idle must never read as hot.
const COOL_BELOW_C: f64 = 70.0;
const HOT_OVER_C: f64 = 92.0;
/// Comfort is full up to the top of the measured idle, and falls to nothing
/// at the Ivy Bridge's TjMax (105 C); at `HOT_OVER_C` it is just under 0.6,
/// where `body_drives::tiredness` starts to count heat.
const COMFORT_FULL_TO_C: f64 = 82.0;
const COMFORT_ZERO_AT_C: f64 = 105.0;

fn temperature_word(temp: f64) -> &'static str {
    if temp < COOL_BELOW_C { ", cool" } else if temp <= HOT_OVER_C { ", warm" } else { ", hot" }
}

fn comfort_of(temp: f64) -> f64 {
    1.0 - (temp - COMFORT_FULL_TO_C).max(0.0) / (COMFORT_ZERO_AT_C - COMFORT_FULL_TO_C)
}

impl Body {
    pub fn new(now: f64) -> Body {
        Body {
            battery: Some(100.0),
            charging: Some(true),
            on_ac: Some(true),
            energy_wh: None,
            power_w: None,
            voltage_v: None,
            health_pct: None,
            cycles: None,
            temp: Some(45.0),
            lux: Some(150.0),
            work: body_work::Reading::default(),
            lid_closed: Some(false),
            lid_seq: 0,
            care: Battery::new(battery::Thresholds::default()),
            mind_saved: false,
            power_off_due: false,
            recover_panel: false,
            panel_ready: false,
            hour: None,
            utc_offset: None,
            clock_trusted: true,
            note: String::new(),
            dirty: true,
            last_pub: now - PERIOD,
            last_contact: now,
            last_stimulus: now,
        }
    }

    pub fn physical(now: f64) -> Body {
        Body {
            battery: None,
            charging: None,
            on_ac: None,
            temp: None,
            lux: None,
            lid_closed: None,
            clock_trusted: false,
            ..Body::new(now)
        }
    }

    #[cfg(any(target_os = "linux", test))]
    pub fn set_power(&mut self, battery: Option<f64>, charging: Option<bool>, on_ac: Option<bool>) {
        if (self.battery, self.charging, self.on_ac) != (battery, charging, on_ac) {
            (self.battery, self.charging, self.on_ac) = (battery, charging, on_ac);
            self.dirty = true;
        }
    }

    /// Battery wear changes over months, so a change is published at once
    /// but costs nothing in between.
    #[cfg(any(target_os = "linux", test))]
    pub fn set_wear(&mut self, health_pct: Option<f64>, cycles: Option<i64>) {
        if (self.health_pct, self.cycles) != (health_pct, cycles) {
            (self.health_pct, self.cycles) = (health_pct, cycles);
            self.dirty = true;
        }
    }

    #[cfg(any(target_os = "linux", test))]
    pub fn set_temperature(&mut self, temp: Option<f64>) {
        if self.temp != temp {
            self.temp = temp;
            self.dirty = true;
        }
    }

    #[cfg(any(target_os = "linux", test))]
    pub fn set_lux(&mut self, lux: Option<f64>) {
        if self.lux != lux {
            self.lux = lux;
            self.dirty = true;
        }
    }

    /// Take one reading from the hardware sensor thread.
    #[cfg(any(target_os = "linux", test))]
    pub fn sense(&mut self, reading: &sysfs::Reading) {
        self.set_power(reading.battery, reading.charging, reading.on_ac);
        // Energy and draw change every reading; they are published with the
        // next periodic state, not as a change of their own.
        (self.energy_wh, self.power_w, self.voltage_v) = (reading.energy_wh, reading.power_w, reading.voltage_v);
        self.set_wear(reading.health_pct, reading.cycles);
        self.set_temperature(reading.temp);
        self.set_lux(reading.lux);
        // A new incident or the start/end of strain matters immediately.
        if self.work.strain_pct.map(|pct| pct > 0.0) != reading.work.strain_pct.map(|pct| pct > 0.0)
            || self.work.thermal_throttles != reading.work.thermal_throttles
            || self.work.oom_organs != reading.work.oom_organs
        {
            self.dirty = true;
        }
        self.work = reading.work;
    }

    pub fn set_lid(&mut self, closed: Option<bool>) {
        if closed != self.lid_closed {
            self.lid_closed = closed;
            self.recover_panel = false;
            self.lid_seq += 1;
            self.panel_ready = false;
            self.dirty = true;
        }
    }

    pub fn recover_panel(&mut self) {
        if self.lid_closed.is_none() && !self.recover_panel {
            self.lid_seq += 1;
            self.recover_panel = true;
            self.panel_ready = false;
        }
    }

    pub fn panel_ready(&mut self, seq: u64) {
        if seq == self.lid_seq && (!self.lid_blocked() || self.recover_panel) {
            self.panel_ready = true;
        }
    }

    #[cfg(target_os = "linux")]
    pub fn panel_blank(&self) -> bool {
        !self.panel_ready
    }

    pub fn lid_seq(&self) -> u64 {
        self.lid_seq
    }

    /// After a restart, carry on numbering lid readings past the last one
    /// organs may still hold, so an old `lid_seq` can never match a new one.
    pub fn continue_lid_seq(&mut self, after: u64) {
        if after >= self.lid_seq {
            self.lid_seq = after.saturating_add(1);
            self.panel_ready = false;
            self.dirty = true;
        }
    }

    /// The lid reading: closed, open or unknown.
    pub fn lid(&self) -> Option<bool> {
        self.lid_closed
    }

    /// The mind has saved its context before a power-off.
    /// Only an answer to a shutdown already announced counts: a stray or
    /// early receipt must never cut a later notice's grace short.
    pub fn mind_saved(&mut self) {
        if self.care.shutting_down().is_some() {
            self.mind_saved = true;
        }
    }

    /// True once, when the battery's keeper decides the power must go.
    pub fn take_power_off(&mut self) -> bool {
        std::mem::take(&mut self.power_off_due)
    }

    fn power_sample(&self) -> battery::Sample {
        battery::Sample {
            percent: self.battery,
            charging: self.charging,
            on_ac: self.on_ac,
            energy_wh: self.energy_wh,
            power_w: self.power_w,
            voltage_v: self.voltage_v,
        }
    }

    pub fn lid_blocked(&self) -> bool {
        self.lid_closed != Some(false)
    }

    pub fn lid_confirmed_closed(&self) -> bool {
        self.lid_closed == Some(true)
    }

    pub fn lid_event(&self) -> Event {
        Event::new("body.lid")
            .with("closed", self.lid_closed)
            .with("seq", self.lid_seq)
            .with("show_face", self.recover_panel)
    }

    pub fn on_event(&mut self, ev: &Event, now: f64) {
        match ev.topic.as_str() {
            "sim.body" => {
                if let Some(v) = ev.f64("battery") {
                    self.battery = Some(v.clamp(0.0, 100.0));
                }
                if let Some(v) = ev.bool("charging") {
                    self.charging = Some(v);
                    self.on_ac = Some(v);
                }
                if let Some(v) = ev.bool("on_ac") {
                    self.on_ac = Some(v);
                }
                if let Some(v) = ev.f64("temp") {
                    self.temp = Some(v.clamp(-20.0, 120.0));
                }
                if let Some(v) = ev.f64("lux") {
                    self.lux = Some(v.max(0.0));
                }
                // A pretend battery draw, so the lab can rehearse time to empty.
                if let Some(v) = ev.f64("energy_wh") {
                    self.energy_wh = Some(v.max(0.0));
                }
                if let Some(v) = ev.f64("power_w") {
                    self.power_w = Some(v.max(0.0));
                }
                if let Some(v) = ev.f64("voltage") {
                    self.voltage_v = Some(v.max(0.0));
                }
                match ev.data.get("lid_closed") {
                    Some(serde_json::Value::Null) => self.set_lid(None),
                    Some(serde_json::Value::Bool(closed)) => self.set_lid(Some(*closed)),
                    _ => {}
                }
                // A pretend worn battery, so the lab can rehearse "battery health".
                if let Some(v) = ev.f64("health_pct") {
                    self.health_pct = Some(v.clamp(0.0, 100.0).round());
                }
                if let Some(v) = ev.data.get("cycles").and_then(serde_json::Value::as_i64) {
                    self.cycles = Some(v.max(0));
                }
                if let Some(v) = ev.f64("hour") {
                    self.hour = if (0.0..24.0).contains(&v) { Some(v as u32) } else { None };
                }
                self.dirty = true;
            }
            "hear.final" | "say.started" => {
                self.last_contact = now;
                self.last_stimulus = now;
            }
            "hear.wake" | "hear.speech_start" | "key.interrupt" | "key.attend" => self.last_stimulus = now,
            _ => {}
        }
    }

    pub fn dark(&self) -> bool {
        self.lux.is_some_and(|lux| lux < 5.0)
    }

    pub fn clock(&self) -> (u32, u32) {
        let (h, m) = match self.utc_offset {
            Some(offset) => offset_time(unix_seconds(), offset),
            None => local_time(),
        };
        (self.hour.unwrap_or(h), m)
    }

    /// The mind knows the owner's time zone; `None` returns to the system's.
    /// Offsets beyond any real zone are refused.
    pub fn set_utc_offset(&mut self, offset: Option<i64>) {
        let offset = offset.filter(|seconds| (-(16 * 3600)..=(16 * 3600)).contains(seconds));
        if offset != self.utc_offset {
            self.utc_offset = offset;
            self.dirty = true;
        }
    }

    pub fn set_clock_trusted(&mut self, trusted: bool) {
        if trusted != self.clock_trusted {
            self.clock_trusted = trusted;
            self.dirty = true;
        }
    }

    pub fn clock_trusted(&self) -> bool {
        self.clock_trusted
    }

    pub fn night(&self) -> bool {
        let (h, _) = self.clock();
        self.clock_trusted && !(7..22).contains(&h)
    }

    fn low_battery(&self) -> bool {
        self.on_ac == Some(false)
            && (self.battery.is_some_and(|battery| battery <= self.care.th.low_pct)
                || self.care.level() >= battery::Level::Low)
    }

    /// One short line of true facts, for the mind to show the model.
    pub fn senses_line(&self) -> String {
        let (h, m) = self.clock();
        let mut line = if self.clock_trusted { format!("{h:02}:{m:02}") } else { "clock uncertain".into() };
        if let Some(battery) = self.battery {
            line.push_str(&format!(", battery {battery:.0}%"));
            if self.low_battery() {
                line.push_str(" and low, unplugged");
                if let Some(minutes) = self.care.minutes_left() {
                    line.push_str(&format!(", about {minutes:.0} min left"));
                }
            } else if self.charging == Some(true) {
                line.push_str(" charging");
            } else if let Some(on_ac) = self.on_ac {
                line.push_str(if on_ac { " plugged in" } else { " unplugged" });
            }
        }
        if let Some(temp) = self.temp {
            line.push_str(temperature_word(temp));
        }
        if let Some(lux) = self.lux {
            line.push_str(if self.dark() { ", room dark" } else if lux < 50.0 { ", room dim" } else { ", room bright" });
        }
        if !self.note.is_empty() {
            line.push_str(", ");
            line.push_str(&self.note);
        }
        line
    }

    pub fn tick(&mut self, now: f64) -> Vec<Event> {
        let mut out = Vec::new();
        let level = self.care.level();
        let (said, power_off) = self.care.update(&self.power_sample(), self.lid_closed, self.mind_saved, now);
        if power_off {
            self.power_off_due = true;
        }
        if self.care.shutting_down().is_none() {
            // Cancelled, done or never begun: the next notice needs a new receipt.
            self.mind_saved = false;
        }
        if self.care.level() != level {
            self.dirty = true;
        }
        if self.dirty || now - self.last_pub >= PERIOD {
            self.dirty = false;
            self.last_pub = now;
            out.push(self.snapshot());
            out.push(self.senses());
            let round = |v: f64| (v.clamp(0.0, 1.0) * 100.0).round() / 100.0;
            let energy = if self.on_ac == Some(true) { Some(1.0) } else { self.battery.map(|v| round(v / 100.0)) };
            let social = round((-(now - self.last_contact) / 3600.0).exp());
            let comfort = self.temp.map(|v| round(comfort_of(v)));
            out.push(
                Event::new("body.drives")
                    .with("energy", energy)
                    .with("arousal", round((-(now - self.last_stimulus) / 60.0).exp()))
                    .with("social", social)
                    .with("comfort", comfort)
                    .with("tiredness", bus::body_drives::tiredness(energy.map(|v| v as f32), comfort.map(|v| v as f32)))
                    .with("eagerness", bus::body_drives::eagerness(Some(social as f32))),
            );
        }
        out.extend(said);
        out
    }

    pub fn snapshot(&self) -> Event {
        let (h, m) = self.clock();
        Event::new("body.state")
            .with("battery", self.battery)
            .with("battery_low", self.low_battery())
            .with("battery_level", if self.on_ac == Some(false) { Some(self.care.level().name()) } else { None })
            .with("minutes_left", self.care.minutes_left().map(f64::round))
            .with("charging", self.charging)
            .with("on_ac", self.on_ac)
            .with("health_pct", self.health_pct)
            .with("cycles", self.cycles)
            .with("temp", self.temp)
            .with("lux", self.lux)
            .with("reflex_strain_pct", self.work.strain_pct)
            .with("package_w", self.work.package_w)
            .with("effort", self.work.effort)
            .with("thermal_throttles", self.work.thermal_throttles)
            .with("oom_organs", self.work.oom_organs)
            .with("lid_closed", self.lid_closed)
            .with("hour", h)
            .with("minute", m)
    }

    pub fn senses(&self) -> Event {
        Event::new("body.senses").with("line", self.senses_line())
    }
}

/// The hardware body as Linux shows it in sysfs, written for the 13-inch
/// mid-2012 MacBook Pro (MacBookPro9,2) and tolerant of anything else:
///
/// - power: ACPI battery `BAT0` (type Battery) and AC adapter `ADP1` (type
///   Mains) under /sys/class/power_supply. The charge is charge_now over
///   charge_full (or the energy pair): this Mac's Smart Battery driver
///   (`sbs`) reports `capacity` against the design capacity, so a full pack
///   worn to 80% read "80". `capacity` is the fallback. The battery's files
///   are read slowly (see `PowerPoll`): each refresh waits on the embedded
///   controller.
/// - temperature: Intel `coretemp` under /sys/class/hwmon, or failing that the
///   CPU keys (labels TC*) of `applesmc`.
/// - light: applesmc at /sys/devices/platform/applesmc.768/light, "(left,right)".
/// - lid: the ACPI "Lid Switch" input device, which reports SW_LID.
/// - panel: intel_backlight (raw), acpi_video0 (firmware), gmux_backlight or
///   apple_backlight (platform) under /sys/class/backlight.
/// - keyboard light: /sys/class/leds/smc::kbd_backlight.
///
/// Every file may be missing, malformed, unreadable (EIO from a busy SMC) or
/// slow (an ACPI battery read waits on the embedded controller). Nothing here
/// panics; the readings run on their own thread, never on the hub's.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub mod sysfs {
    use crate::body_work;
    use std::collections::HashSet;
    use std::fs;
    use std::io::Read;
    use std::path::{Path, PathBuf};
    use std::time::{Duration, Instant};

    /// A sysfs value larger than this is not a sensor value.
    const MAX_VALUE: u64 = 4096;
    /// More entries than this in one sysfs directory is not a laptop.
    const MAX_ENTRIES: usize = 256;
    /// A reading that fails briefly keeps its last good value this long, so a
    /// busy SMC or embedded controller does not make the body flicker.
    const HOLD: Duration = Duration::from_secs(10);
    /// Approximate lux for one applesmc light-sensor step. The sensor is not
    /// calibrated; this only has to tell a dark room (0 or 1) from a lit one.
    pub const LUX_PER_STEP: f64 = 4.0;
    /// A pass over every sensor slower than this is worth one log line.
    const SLOW_PASS: Duration = Duration::from_millis(500);
    /// The device survey is logged at start and once more this much later.
    #[cfg(target_os = "linux")]
    const RESURVEY: Duration = Duration::from_secs(20);
    /// How often the battery's own files are read. On the MacBookPro9,2
    /// every attribute of the ACPI Smart Battery (present, status, charge,
    /// voltage, current) refreshes through the embedded controller once the
    /// driver's 1 s cache has expired: about 126 EC interrupts and 55-64 ms
    /// blocked per refresh (measured 25 November 2026). The adapter's
    /// `online` costs nothing and is read on every pass; a change there
    /// reads the battery at once.
    pub const BATTERY_ON_AC: Duration = Duration::from_secs(60);
    pub const BATTERY_UNPLUGGED: Duration = Duration::from_secs(20);
    /// At or under `BATTERY_LOW_PCT` on battery (or with the charge unknown),
    /// faster, for the pleas and the clean power-off: every judgement there
    /// holds for 30 s (`battery::Thresholds` confirm_hold and floor_hold),
    /// so 10 s still gives it three readings, at half the EC traffic of 5 s
    /// when the pack can least afford it. bodycare reads at 10 s then too.
    pub const BATTERY_LOW: Duration = Duration::from_secs(10);
    pub const BATTERY_LOW_PCT: f64 = 25.0;
    /// A read that a change caused (the adapter, the batteries listed, a
    /// power-supply uevent) is read once more this much later: the gauge's
    /// status can lag the adapter's `online`, and the driver's 1 s cache
    /// can hand back the figures bodycare read at the moment of the change.
    pub const BATTERY_SETTLE: Duration = Duration::from_secs(4);
    /// Uevents never read the battery more often than this.
    pub const BATTERY_POKE_GAP: Duration = Duration::from_secs(2);
    /// Touched by 60-being-power.rules on every power_supply change uevent
    /// (relative to the root). The `sbs` driver keeps `BAT0` listed and
    /// flips `present` to 0 when the pack goes, and the adapter does not
    /// change: only the uevent tells. A stat of it costs no EC traffic.
    pub const POWER_STAMP: &str = "run/being-power-changed";

    /// When the power supplies last sent a change uevent; None before any.
    pub fn power_stamp(root: &Path) -> Option<std::time::SystemTime> {
        fs::metadata(root.join(POWER_STAMP)).and_then(|m| m.modified()).ok()
    }

    #[derive(Clone, Copy, Debug, Default, PartialEq)]
    pub struct Reading {
        pub battery: Option<f64>,
        pub charging: Option<bool>,
        pub on_ac: Option<bool>,
        pub temp: Option<f64>,
        pub lux: Option<f64>,
        /// Energy left in the battery, watt-hours.
        pub energy_wh: Option<f64>,
        /// The battery's draw, watts (only meaningful while discharging).
        pub power_w: Option<f64>,
        /// The pack voltage, volts.
        pub voltage_v: Option<f64>,
        /// Full charge now over full charge when new, percent.
        pub health_pct: Option<f64>,
        /// Charge cycles the battery has counted.
        pub cycles: Option<i64>,
        pub work: body_work::Reading,
    }

    impl Reading {
        pub fn describe(&self) -> String {
            fn yes(value: Option<bool>) -> &'static str {
                match value {
                    Some(true) => "yes",
                    Some(false) => "no",
                    None => "unknown",
                }
            }
            let or_unknown = |value: Option<f64>, unit: &str| {
                value.map_or("unknown".to_owned(), |v| format!("{v:.0}{unit}"))
            };
            format!(
                "battery {}, charging {}, on AC {}, CPU {}, light {}",
                or_unknown(self.battery, "%"),
                yes(self.charging),
                yes(self.on_ac),
                or_unknown(self.temp, " C"),
                or_unknown(self.lux, " lux (approx.)"),
            )
        }
    }

    /// Log lines said at most once each, so a broken device is reported once
    /// and then ignored instead of filling the journal.
    #[derive(Default)]
    pub struct Notes {
        said: HashSet<String>,
        lines: Vec<String>,
    }

    impl Notes {
        pub fn once(&mut self, key: impl Into<String>, line: impl FnOnce() -> String) {
            if self.said.len() < 4 * MAX_ENTRIES && self.said.insert(key.into()) {
                self.lines.push(line());
            }
        }

        pub fn take(&mut self) -> Vec<String> {
            std::mem::take(&mut self.lines)
        }
    }

    /// The trimmed text of a small sysfs file, or None if it is missing,
    /// unreadable, empty, too big or not text.
    pub fn read_text(path: &Path) -> Option<String> {
        let mut data = String::new();
        fs::File::open(path).ok()?.take(MAX_VALUE).read_to_string(&mut data).ok()?;
        let data = data.trim();
        (!data.is_empty()).then(|| data.to_owned())
    }

    fn read_number(path: &Path) -> Option<i64> {
        read_text(path)?.parse().ok()
    }

    /// The sorted entries of a directory; None if it cannot be listed.
    fn list(dir: &Path) -> Option<Vec<PathBuf>> {
        let mut entries: Vec<_> = fs::read_dir(dir).ok()?.flatten().take(MAX_ENTRIES).map(|e| e.path()).collect();
        entries.sort();
        Some(entries)
    }

    fn name_of(path: &Path) -> String {
        path.file_name().map_or_else(String::new, |name| name.to_string_lossy().into_owned())
    }

    #[derive(Clone, Copy, Default, Debug, PartialEq)]
    pub struct Power {
        pub battery: Option<f64>,
        pub charging: Option<bool>,
        pub on_ac: Option<bool>,
        pub energy_wh: Option<f64>,
        pub power_w: Option<f64>,
        pub voltage_v: Option<f64>,
        pub health_pct: Option<f64>,
        pub cycles: Option<i64>,
    }

    /// Battery wear: full charge (or energy) now over full when new, as a
    /// whole percent, and the cycle count. A worn pack can report a little
    /// over its design figure when new; that is shown as 100. A cycle count
    /// of 0 is what many controllers report when they do not count, so it is
    /// unknown rather than "brand new".
    pub fn battery_wear(dir: &Path) -> (Option<f64>, Option<i64>) {
        let health = ["charge", "energy"].into_iter().find_map(|kind| {
            let full = read_number(&dir.join(format!("{kind}_full"))).filter(|v| *v > 0)?;
            let design = read_number(&dir.join(format!("{kind}_full_design"))).filter(|v| *v > 0)?;
            Some((full as f64 * 100.0 / design as f64).round().clamp(0.0, 100.0))
        });
        let cycles = read_number(&dir.join("cycle_count")).filter(|c| (1..100_000).contains(c));
        (health, cycles)
    }

    /// Energy left, draw and voltage, from energy_* and power_now, or from
    /// charge_* and current_now times the voltage. Units in sysfs are micro
    /// (µWh, µW, µV, µAh, µA). Some batteries sign the current; its size is
    /// what matters.
    fn battery_energy(dir: &Path) -> (Option<f64>, Option<f64>, Option<f64>) {
        let micro = |name: &str| read_number(&dir.join(name)).map(|v| v as f64 / 1e6);
        let volts = micro("voltage_now").filter(|v| *v > 0.0 && *v < 30.0);
        let energy = micro("energy_now")
            .filter(|e| *e >= 0.0)
            .or_else(|| Some(micro("charge_now")? * volts?).filter(|e| *e >= 0.0));
        let power = micro("power_now")
            .map(f64::abs)
            .or_else(|| Some(micro("current_now")?.abs() * volts?))
            .filter(|w| *w < 200.0);
        (energy.filter(|e| *e < 500.0), power, volts)
    }

    /// Percent charge: how full the pack is against what it holds when full
    /// today. Energy or charge now over full comes first; `capacity` only
    /// when neither pair is there. On the laptop's Smart Battery `capacity`
    /// is the AbsoluteStateOfCharge (against the design): measured status
    /// Full, capacity 80, charge_now 4593000, charge_full 4629000. A pack
    /// that says Full and is within 3 points of full is 100: the gauge
    /// stopped the charge. Battery health stays separate (`battery_wear`).
    pub fn battery_percent(dir: &Path) -> Option<f64> {
        let ratio = ["energy", "charge"].into_iter().find_map(|kind| {
            let now = read_number(&dir.join(format!("{kind}_now"))).filter(|v| *v >= 0)?;
            let full = read_number(&dir.join(format!("{kind}_full"))).filter(|v| *v > 0)?;
            // A worn battery can report more than its last full charge.
            Some((now as f64 * 100.0 / full as f64).clamp(0.0, 100.0))
        });
        match ratio {
            Some(r) if r >= 97.0 && read_text(&dir.join("status")).as_deref() == Some("Full") => Some(100.0),
            Some(r) => Some(r.round()),
            None => read_number(&dir.join("capacity")).filter(|p| (0..=100).contains(p)).map(|p| p as f64),
        }
    }

    /// The cheap half of a power reading: whether the mains is on (the
    /// adapters' `online`, which the drivers answer from their own state)
    /// and which directories are the body's batteries (`type` costs
    /// nothing either). None when the power supplies cannot be listed.
    pub struct Supplies {
        pub on_ac: Option<bool>,
        pub batteries: Vec<PathBuf>,
    }

    pub fn read_supplies(root: &Path, notes: &mut Notes) -> Option<Supplies> {
        let Some(devices) = list(&root.join("sys/class/power_supply")) else {
            notes.once("power:none", || "power: no power_supply devices; battery and AC unknown".into());
            return None;
        };
        let mut supplies = Supplies { on_ac: None, batteries: Vec::new() };
        let mut ac_unreadable = false;
        for path in devices {
            let name = name_of(&path);
            let key = format!("power:{name}");
            // A wireless mouse's or keyboard's own battery is not the body's.
            if read_text(&path.join("scope")).as_deref() == Some("Device") {
                notes.once(key, || format!("power: ignoring {name}, a peripheral's battery"));
                continue;
            }
            match read_text(&path.join("type")).as_deref() {
                Some("Battery") => {
                    notes.once(key, || format!("power: found battery {name}"));
                    supplies.batteries.push(path);
                }
                Some("Mains") => {
                    notes.once(key, || format!("power: found AC adapter {name}"));
                    match read_text(&path.join("online")).as_deref() {
                        Some("1") => supplies.on_ac = Some(true),
                        Some("0") if supplies.on_ac.is_none() => supplies.on_ac = Some(false),
                        Some("0") => {}
                        _ => ac_unreadable = true,
                    }
                }
                Some(other) => {
                    let other = other.to_owned();
                    notes.once(key, || format!("power: ignoring {name} of type {other}"));
                }
                None => notes.once(key, || format!("power: ignoring {name}, its type is unreadable")),
            }
        }
        // One adapter saying "off" is not proof while another cannot be read.
        if ac_unreadable && supplies.on_ac == Some(false) {
            supplies.on_ac = None;
        }
        Some(supplies)
    }

    /// The expensive half: the first present battery's charge, energy, draw,
    /// wear and status (`on_ac` is left unknown), and which battery it was.
    pub fn read_battery(batteries: &[PathBuf], notes: &mut Notes) -> (Power, Option<PathBuf>) {
        let mut power = Power::default();
        for path in batteries {
            let name = name_of(path);
            if read_text(&path.join("present")).as_deref() == Some("0") {
                notes.once(format!("power:{name}:absent"), || format!("power: battery {name} is not present"));
                continue;
            }
            power.battery = battery_percent(path);
            (power.energy_wh, power.power_w, power.voltage_v) = battery_energy(path);
            (power.health_pct, power.cycles) = battery_wear(path);
            if power.battery.is_none() {
                notes.once(format!("power:{name}:unreadable"), || format!("power: battery {name} reports no readable charge"));
            }
            power.charging = match read_text(&path.join("status")).as_deref() {
                Some("Charging") => Some(true),
                Some("Discharging" | "Full" | "Not charging") => Some(false),
                _ => None,
            };
            return (power, Some(path.clone()));
        }
        (power, None)
    }

    /// Everything at once: the adapters and the battery.
    pub fn read_power(root: &Path, notes: &mut Notes) -> Power {
        let Some(supplies) = read_supplies(root, notes) else { return Power::default() };
        let (power, _) = read_battery(&supplies.batteries, notes);
        Power { on_ac: supplies.on_ac, ..power }
    }

    /// Reads the power supplies on every pass, but the battery's own files
    /// only every `BATTERY_ON_AC` / `BATTERY_UNPLUGGED` / `BATTERY_LOW`, at
    /// once when the adapter or the batteries listed change or a
    /// power-supply uevent comes (`POWER_STAMP`), once more `BATTERY_SETTLE`
    /// after such a read, and again soon after a read that found no charge.
    /// Figures are only kept while their battery is still listed.
    #[derive(Default)]
    pub struct PowerPoll {
        last: Option<(Instant, Option<bool>)>,
        battery: Power,
        /// The battery the figures came from, and every battery listed then.
        from: Option<PathBuf>,
        listed: Vec<PathBuf>,
        /// The uevent stamp as it was at the last read.
        stamp: Option<std::time::SystemTime>,
        /// The last read was caused by a change: one follow-up is due.
        follow_up: bool,
        /// How many times the battery's files have been read.
        pub reads: u64,
    }

    impl PowerPoll {
        fn every(&self) -> Duration {
            let low = self.battery.battery.is_none_or(|p| p <= BATTERY_LOW_PCT);
            match self.last.map(|(_, on_ac)| on_ac) {
                _ if self.from.is_some() && self.battery.battery.is_none() => HOLD,
                Some(Some(true)) => BATTERY_ON_AC,
                _ if low => BATTERY_LOW,
                _ => BATTERY_UNPLUGGED,
            }
        }

        /// The adapter or the batteries listed changed since the last read,
        /// or a uevent came. None before the first read.
        fn changed(&self, on_ac: Option<bool>, batteries: &[PathBuf], poked: bool) -> Option<bool> {
            let (_, then) = self.last?;
            Some(then != on_ac || self.listed != batteries || poked)
        }

        fn due(&self, on_ac: Option<bool>, batteries: &[PathBuf], poked: bool, now: Instant) -> bool {
            let Some((at, then)) = self.last else { return true };
            if then != on_ac || self.listed != batteries || now < at {
                return true;
            }
            let since = now.saturating_duration_since(at);
            (poked && since >= BATTERY_POKE_GAP) || (self.follow_up && since >= BATTERY_SETTLE) || since >= self.every()
        }

        pub fn read(&mut self, root: &Path, notes: &mut Notes, now: Instant) -> Power {
            let Some(supplies) = read_supplies(root, notes) else {
                *self = PowerPoll { reads: self.reads, ..PowerPoll::default() };
                return Power::default();
            };
            let stamp = power_stamp(root);
            let poked = stamp != self.stamp;
            if self.due(supplies.on_ac, &supplies.batteries, poked, now) {
                self.follow_up = self.changed(supplies.on_ac, &supplies.batteries, poked) == Some(true);
                (self.battery, self.from) = read_battery(&supplies.batteries, notes);
                self.last = Some((now, supplies.on_ac));
                self.listed = supplies.batteries;
                self.stamp = stamp;
                self.reads += 1;
            }
            Power { on_ac: supplies.on_ac, ..self.battery }
        }
    }

    fn chip_name(chip: &Path) -> Option<String> {
        read_text(&chip.join("name")).or_else(|| read_text(&chip.join("device/name")))
    }

    /// The `tempN_input` files in a directory, as (N, path).
    fn temp_inputs(dir: &Path) -> Vec<(String, PathBuf)> {
        list(dir)
            .unwrap_or_default()
            .into_iter()
            .filter_map(|path| {
                let name = name_of(&path);
                let channel = name.strip_prefix("temp")?.strip_suffix("_input")?;
                (!channel.is_empty() && channel.bytes().all(|b| b.is_ascii_digit()))
                    .then(|| (channel.to_owned(), path))
            })
            .collect()
    }

    /// The coretemp chips' hottest channel: None when there is no coretemp
    /// chip, Some(None) when one exists but a channel cannot be trusted.
    fn coretemp(hwmon: &Path) -> Option<Option<f64>> {
        let mut found = false;
        let mut hottest: Option<i64> = None;
        for chip in list(hwmon)? {
            if chip_name(&chip).as_deref() != Some("coretemp") {
                continue;
            }
            let mut dir = chip.clone();
            let mut inputs = temp_inputs(&dir);
            if inputs.is_empty() {
                // Older kernels keep the channels on the platform device.
                dir = chip.join("device");
                inputs = temp_inputs(&dir);
            }
            // Same rule as bodycare: the channel labelled "Package id 0"
            // when there is one, else the hottest of the channels.
            if let Some(package) = inputs
                .iter()
                .find(|(channel, _)| read_text(&dir.join(format!("temp{channel}_label"))).as_deref() == Some("Package id 0"))
                .cloned()
            {
                inputs = vec![package];
            }
            // A coretemp chip with no channels at all is not a reading; let
            // the SMC stand in rather than leaving the temperature unknown.
            found |= !inputs.is_empty();
            for (_, input) in inputs {
                let Some(value) = read_number(&input).filter(|v| (1_000..=150_000).contains(v)) else {
                    return Some(None);
                };
                hottest = Some(hottest.map_or(value, |current| current.max(value)));
            }
        }
        found.then(|| hottest.map(|value| value as f64 / 1000.0))
    }

    /// Read the hottest package/core channel from the Intel coretemp driver.
    /// One unreadable channel invalidates the reading: the hottest may be it.
    #[cfg(test)]
    pub fn read_core_temperature(hwmon: &Path) -> Option<f64> {
        coretemp(hwmon).flatten()
    }

    /// The hottest CPU sensor the SMC reports (labels TC0D, TC0P, ...). SMC
    /// keys are often junk, so a bad channel is skipped rather than trusted.
    pub fn smc_cpu_temperature(smc: &Path) -> Option<f64> {
        temp_inputs(smc)
            .into_iter()
            .filter(|(channel, _)| {
                read_text(&smc.join(format!("temp{channel}_label"))).is_some_and(|label| label.starts_with("TC"))
            })
            .filter_map(|(_, input)| read_number(&input).filter(|v| (1..=150_000).contains(v)))
            .max()
            .map(|value| value as f64 / 1000.0)
    }

    /// The CPU temperature: coretemp if the kernel has it, else the SMC.
    pub fn read_temperature(root: &Path, smc: Option<&Path>) -> Option<f64> {
        match coretemp(&root.join("sys/class/hwmon")) {
            Some(reading) => reading,
            None => smc.and_then(smc_cpu_temperature),
        }
    }

    /// The applesmc platform device, if the kernel found an SMC.
    pub fn find_smc(root: &Path) -> Option<PathBuf> {
        let direct = root.join("sys/devices/platform/applesmc.768");
        if direct.is_dir() {
            return Some(direct);
        }
        list(&root.join("sys/bus/platform/devices"))?
            .into_iter()
            .find(|path| name_of(path).starts_with("applesmc") && path.is_dir())
    }

    /// The brighter of applesmc's two light-sensor values, "(left,right)".
    pub fn parse_light(text: &str) -> Option<u32> {
        let inner = text.trim().strip_prefix('(')?.strip_suffix(')')?;
        let (left, right) = inner.split_once(',')?;
        let left: u32 = left.trim().parse().ok()?;
        let right: u32 = right.trim().parse().ok()?;
        (left <= 4095 && right <= 4095).then(|| left.max(right))
    }

    pub fn read_light(smc: &Path) -> Option<u32> {
        parse_light(&read_text(&smc.join("light"))?)
    }

    /// The input devices, as (eventN, name, reports SW_LID).
    pub fn input_devices(root: &Path) -> Vec<(String, String, bool)> {
        list(&root.join("sys/class/input"))
            .unwrap_or_default()
            .into_iter()
            .filter(|path| name_of(path).strip_prefix("event").is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())))
            .map(|path| {
                let device = path.join("device");
                let name = read_text(&device.join("name")).unwrap_or_else(|| "(no name)".into());
                let lid = read_text(&device.join("capabilities/sw")).is_some_and(|bits| has_lid_switch(&bits));
                (name_of(&path), name, lid)
            })
            .collect()
    }

    /// The kernel prints a capability bitmap as hex words, most significant
    /// first; SW_LID is bit 0 of the last word.
    pub fn has_lid_switch(bitmap: &str) -> bool {
        bitmap
            .split_whitespace()
            .last()
            .and_then(|word| u64::from_str_radix(word, 16).ok())
            .is_some_and(|word| word & 1 != 0)
    }

    /// The backlight devices whose `bl_power` can blank the panel, native GPU
    /// control first, then platform, then firmware (ACPI video).
    pub fn backlight_controls(root: &Path) -> Vec<(String, String, PathBuf)> {
        let mut found: Vec<_> = list(&root.join("sys/class/backlight"))
            .unwrap_or_default()
            .into_iter()
            .map(|path| {
                let kind = read_text(&path.join("type")).unwrap_or_else(|| "unknown".into());
                let rank = match kind.as_str() {
                    "raw" => 0,
                    "platform" => 1,
                    "firmware" => 2,
                    _ => 3,
                };
                (rank, name_of(&path), kind, path.join("bl_power"))
            })
            .collect();
        found.sort();
        found.into_iter().map(|(_, name, kind, control)| (name, kind, control)).collect()
    }

    /// One line per device the body might use, for the boot log and for
    /// "Check hardware": did it find the lid, battery, light sensor, backlight?
    pub fn survey(root: &Path) -> Vec<String> {
        let mut lines = Vec::new();
        let inputs = input_devices(root);
        if inputs.is_empty() {
            lines.push("input: no input devices yet".into());
        }
        for (event, name, _) in &inputs {
            lines.push(format!("input: {event} \"{name}\""));
        }
        match inputs.iter().find(|(_, _, lid)| *lid) {
            Some((event, name, _)) => lines.push(format!("lid: found \"{name}\" ({event})")),
            None => lines.push("lid: no lid switch found; the lid stays unknown".into()),
        }

        let mut notes = Notes::default();
        let power = read_power(root, &mut notes);
        lines.extend(notes.take());
        if power.battery.is_none() && power.on_ac.is_none() {
            lines.push("power: no battery or AC reading".into());
        }

        let backlights = backlight_controls(root);
        if backlights.is_empty() {
            lines.push("backlight: none found; the panel cannot be blanked".into());
        }
        for (name, kind, control) in backlights {
            let dir = control.parent().map(Path::to_path_buf).unwrap_or_default();
            let level = |file: &str| read_text(&dir.join(file)).unwrap_or_else(|| "?".into());
            lines.push(format!(
                "backlight: {name} ({kind}), brightness {}/{}, bl_power {}",
                level("brightness"),
                level("max_brightness"),
                if control.exists() { level("bl_power") } else { "missing".into() },
            ));
        }

        let leds = list(&root.join("sys/class/leds")).unwrap_or_default();
        match leds.iter().find(|path| name_of(path).ends_with("kbd_backlight")) {
            Some(led) => lines.push(format!(
                "keyboard light: {}, brightness {}/{}",
                name_of(led),
                read_text(&led.join("brightness")).unwrap_or_else(|| "?".into()),
                read_text(&led.join("max_brightness")).unwrap_or_else(|| "?".into()),
            )),
            None => lines.push("keyboard light: not found".into()),
        }

        let smc = find_smc(root);
        match &smc {
            Some(smc) => {
                let fans = list(smc).unwrap_or_default().iter()
                    .filter(|p| { let n = name_of(p); n.starts_with("fan") && n.ends_with("_input") }).count();
                lines.push(format!("smc: found {}, {} temperature channels, {fans} fans", name_of(smc), temp_inputs(smc).len()));
                lines.push(match (read_text(&smc.join("light")), read_light(smc)) {
                    (_, Some(raw)) => format!("light sensor: found, raw {raw} (~{:.0} lux)", f64::from(raw) * LUX_PER_STEP),
                    (Some(text), None) => format!("light sensor: unreadable value {text:?}"),
                    (None, None) => "light sensor: not found or not answering".into(),
                });
            }
            None => lines.push("smc: applesmc not found; no light sensor or SMC temperatures".into()),
        }

        let chips: Vec<_> = list(&root.join("sys/class/hwmon")).unwrap_or_default().iter()
            .map(|chip| chip_name(chip).unwrap_or_else(|| "?".into())).collect();
        lines.push(if chips.is_empty() { "temperature: no hwmon chips".into() } else { format!("temperature: hwmon {}", chips.join(", ")) });
        lines
    }

    fn hold<T: Copy>(fresh: Option<T>, last: &mut Option<(T, Instant)>, now: Instant) -> Option<T> {
        match fresh {
            Some(value) => {
                *last = Some((value, now));
                Some(value)
            }
            None => {
                let kept = last.filter(|(_, at)| now.saturating_duration_since(*at) < HOLD).map(|(value, _)| value);
                if kept.is_none() {
                    *last = None;
                }
                kept
            }
        }
    }

    /// The sensors read on the sensor thread, with short failures smoothed.
    pub struct Sensors {
        root: PathBuf,
        pub notes: Notes,
        pub power: PowerPoll,
        battery: Option<(f64, Instant)>,
        charging: Option<(bool, Instant)>,
        on_ac: Option<(bool, Instant)>,
        temp: Option<(f64, Instant)>,
        lux: Option<(f64, Instant)>,
        energy: Option<(f64, Instant)>,
        draw: Option<(f64, Instant)>,
        volts: Option<(f64, Instant)>,
        health: Option<(f64, Instant)>,
        cycles: Option<(i64, Instant)>,
        work: body_work::Counters,
    }

    impl Sensors {
        pub fn new(root: PathBuf) -> Self {
            Self {
                root,
                notes: Notes::default(),
                power: PowerPoll::default(),
                battery: None,
                charging: None,
                on_ac: None,
                temp: None,
                lux: None,
                energy: None,
                draw: None,
                volts: None,
                health: None,
                cycles: None,
                work: body_work::Counters::default(),
            }
        }

        pub fn read(&mut self, now: Instant) -> Reading {
            let power = self.power.read(&self.root, &mut self.notes, now);
            let smc = find_smc(&self.root);
            let temp = read_temperature(&self.root, smc.as_deref());
            if temp.is_none() {
                self.notes.once("temp:none", || "temperature: no CPU temperature reading".into());
            }
            let raw = smc.as_deref().and_then(read_light);
            if let Some(raw) = raw {
                self.notes.once("light:first", || format!("light sensor: first reading raw {raw}"));
            } else {
                self.notes.once("light:none", || "light sensor: no reading; the room's light is unknown".into());
            }
            let lux = raw.map(|raw| f64::from(raw) * LUX_PER_STEP);
            Reading {
                battery: hold(power.battery, &mut self.battery, now),
                charging: hold(power.charging, &mut self.charging, now),
                on_ac: hold(power.on_ac, &mut self.on_ac, now),
                temp: hold(temp, &mut self.temp, now),
                lux: hold(lux, &mut self.lux, now),
                energy_wh: hold(power.energy_wh, &mut self.energy, now),
                power_w: hold(power.power_w, &mut self.draw, now),
                voltage_v: hold(power.voltage_v, &mut self.volts, now),
                health_pct: hold(power.health_pct, &mut self.health, now),
                cycles: hold(power.cycles, &mut self.cycles, now),
                work: self.work.read(&self.root, now),
            }
        }

        /// One timed pass, noting once if the hardware is slow to answer.
        pub fn pass(&mut self) -> Reading {
            let started = Instant::now();
            let reading = self.read(started);
            let took = started.elapsed();
            if took > SLOW_PASS {
                self.notes.once("slow", || format!("sensors: one reading pass took {} ms", took.as_millis()));
            }
            reading
        }
    }

    /// Read the sensors every `period` on a thread of their own. The hub only
    /// ever takes what is already there, so a stuck read cannot stall it.
    #[cfg(target_os = "linux")]
    pub fn spawn(root: PathBuf, period: Duration) -> Option<std::sync::mpsc::Receiver<Reading>> {
        use std::sync::mpsc::{sync_channel, TrySendError};
        let (tx, rx) = sync_channel(1);
        let thread = std::thread::Builder::new().name("being-sensors".into()).spawn(move || {
            let started = Instant::now();
            let mut said = HashSet::new();
            for line in survey(&root) {
                eprintln!("brainstem: hardware: {line}");
                said.insert(line);
            }
            // USB input (the internal keyboard and trackpad) and late drivers
            // can appear after the brainstem starts: survey once more and
            // log only what changed, so the boot log is not a stale answer.
            let mut resurveyed = false;
            let mut sensors = Sensors::new(root.clone());
            loop {
                if !resurveyed && started.elapsed() >= RESURVEY {
                    resurveyed = true;
                    for line in survey(&root) {
                        if !said.contains(&line) {
                            eprintln!("brainstem: hardware: (after {} s) {line}", RESURVEY.as_secs());
                            said.insert(line);
                        }
                    }
                }
                let reading = sensors.pass();
                for line in sensors.notes.take() {
                    eprintln!("brainstem: {line}");
                }
                if let Err(TrySendError::Disconnected(_)) = tx.try_send(reading) {
                    return;
                }
                std::thread::sleep(period);
            }
        });
        match thread {
            Ok(_) => Some(rx),
            Err(e) => {
                eprintln!("brainstem: sensors: could not start the sensor thread: {e}");
                None
            }
        }
    }

    /// A reading older than this is no longer the truth.
    pub const STALE: f64 = 30.0;

    /// The hub's end of the sensor thread.
    #[cfg(any(target_os = "linux", test))]
    pub struct Feed {
        rx: std::sync::mpsc::Receiver<Reading>,
        at: f64,
        quiet: bool,
    }

    #[cfg(any(target_os = "linux", test))]
    impl Feed {
        pub fn new(rx: std::sync::mpsc::Receiver<Reading>, now: f64) -> Self {
            Self { rx, at: now, quiet: false }
        }

        /// Apply the newest reading without waiting. If the thread has
        /// stopped or gone quiet, the readings become unknown, said once.
        pub fn update(&mut self, body: &mut super::Body, now: f64) -> Option<&'static str> {
            use std::sync::mpsc::TryRecvError;
            let mut latest = None;
            let mut gone = false;
            loop {
                match self.rx.try_recv() {
                    Ok(reading) => latest = Some(reading),
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => {
                        gone = true;
                        break;
                    }
                }
            }
            if let Some(reading) = latest {
                body.sense(&reading);
                self.at = now;
                self.quiet = false;
                return None;
            }
            if self.quiet || !(gone || now - self.at > STALE) {
                return None;
            }
            self.quiet = true;
            body.sense(&Reading::default());
            Some(if gone {
                "sensors: the sensor thread stopped; battery, temperature and light are now unknown"
            } else {
                "sensors: no reading for 30 s; battery, temperature and light are now unknown"
            })
        }
    }
}

fn unix_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs() as i64)
}

/// Hour and minute of the day at `offset` seconds east of UTC.
fn offset_time(unix: i64, offset: i64) -> (u32, u32) {
    let day = (unix + offset).rem_euclid(86_400);
    ((day / 3600) as u32, (day % 3600 / 60) as u32)
}

fn local_time() -> (u32, u32) {
    // SAFETY: plain libc calls with valid pointers to stack values.
    unsafe {
        let t = libc::time(std::ptr::null_mut());
        let mut tm: libc::tm = std::mem::zeroed();
        libc::localtime_r(&t, &mut tm);
        (tm.tm_hour as u32, tm.tm_min as u32)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_clock_follows_the_zone_the_mind_gives() {
        // 12:30 UTC on some day; Sydney in summer is eleven hours ahead.
        let noon = 20_000 * 86_400 + 12 * 3600 + 30 * 60;
        assert_eq!(offset_time(noon, 0), (12, 30));
        assert_eq!(offset_time(noon, 11 * 3600), (23, 30));
        assert_eq!(offset_time(noon, -(5 * 3600 + 1800)), (7, 0));

        let mut b = Body::new(0.0);
        b.tick(0.0);
        b.set_utc_offset(Some(11 * 3600));
        let (h, m) = offset_time(unix_seconds(), 11 * 3600);
        let (bh, bm) = b.clock();
        assert!((bh, bm) == (h, m) || bm == (m + 1) % 60, "body clock uses the given zone");
        assert!(!b.tick(0.1).is_empty(), "a zone change is published at once");
        b.set_utc_offset(Some(99 * 3600));
        assert_eq!(b.utc_offset, None, "an impossible offset is refused");
    }

    #[test]
    fn senses_line_reports_what_was_set() {
        let mut b = Body::new(0.0);
        b.on_event(
            &Event::new("sim.body")
                .with("battery", 12.0)
                .with("charging", false)
                .with("temp", 95.0)
                .with("lux", 1.0)
                .with("hour", 23.0),
            1.0,
        );
        let line = b.senses_line();
        assert!(line.starts_with("23:"), "{line}");
        assert!(line.contains("battery 12% and low, unplugged"), "{line}");
        assert!(line.contains("hot"), "{line}");
        assert!(line.contains("room dark"), "{line}");
        assert!(b.dark() && b.night());
    }

    #[test]
    fn normal_idle_is_warm_and_comfortable_not_hot() {
        // Measured idle package: 71-82 C. Hot starts over bodycare's 92 C.
        for temp in [71.0, 76.0, 82.0, 92.0] {
            assert_eq!(temperature_word(temp), ", warm", "{temp}");
        }
        assert_eq!(temperature_word(60.0), ", cool");
        assert_eq!(temperature_word(92.5), ", hot");
        for temp in [71.0, 82.0] {
            assert_eq!(comfort_of(temp), 1.0, "{temp}");
        }
        assert!(comfort_of(92.0) < 0.6 && comfort_of(92.0) > 0.5);
        assert!(comfort_of(105.0) <= 0.0);
        let mut b = Body::new(0.0);
        b.on_event(&Event::new("sim.body").with("temp", 80.0), 1.0);
        assert!(!b.senses_line().contains("hot"), "{}", b.senses_line());
        assert_eq!(b.tick(99999.0).iter().find(|e| e.topic == "body.drives").unwrap().f64("comfort"), Some(1.0));
    }

    #[test]
    fn coretemp_reads_the_package_channel_and_rejects_zero() {
        let tree = Tree::new("pkg");
        let root = tree.0.clone();
        tree.file("hwmon0/name", "coretemp\n");
        tree.file("hwmon0/temp1_label", "Package id 0\n");
        tree.file("hwmon0/temp1_input", "72000\n");
        tree.file("hwmon0/temp2_label", "Core 0\n");
        tree.file("hwmon0/temp2_input", "80000\n");
        assert_eq!(sysfs::read_core_temperature(&root), Some(72.0));
        tree.file("hwmon0/temp1_input", "0\n");
        assert_eq!(sysfs::read_core_temperature(&root), None, "a stuck zero is not a cool reading");
    }

    #[test]
    fn publishes_on_change_and_then_periodically() {
        let mut b = Body::new(0.0);
        let first = b.tick(0.1);
        assert_eq!(first.len(), 3);
        assert!(b.tick(1.0).is_empty());
        b.on_event(&Event::new("sim.body").with("lux", 20.0), 1.5);
        assert_eq!(b.tick(1.6).len(), 3);
        assert!(b.tick(3.0).is_empty());
        assert_eq!(b.tick(6.7).len(), 3);
    }

    #[test]
    fn battery_low_requires_a_known_low_charge_and_confirmed_disconnection() {
        for (battery, on_ac, low) in [
            (Some(15.0), Some(false), true),
            (Some(16.0), Some(false), false),
            (Some(9.0), Some(true), false),
            (Some(9.0), None, false),
            (None, Some(false), false),
        ] {
            let mut b = Body::physical(0.0);
            b.set_power(battery, Some(false), on_ac);
            let event = b.tick(1.0).into_iter().find(|e| e.topic == "body.state").unwrap();
            assert_eq!(event.bool("battery_low"), Some(low));
            assert_eq!(b.snapshot().bool("battery_low"), Some(low));
        }
        // Once low on battery, a reading that wobbles back up does not clear
        // it; plugging in does.
        let mut b = Body::physical(0.0);
        b.set_power(Some(15.0), Some(false), Some(false));
        for t in 1..=31 {
            b.tick(f64::from(t));
        }
        b.set_power(Some(16.0), Some(false), Some(false));
        b.tick(32.0);
        assert_eq!(b.snapshot().bool("battery_low"), Some(true));
        b.set_power(Some(16.0), Some(true), Some(true));
        b.tick(33.0);
        assert_eq!(b.snapshot().bool("battery_low"), Some(false));
    }

    #[test]
    fn drives_stay_in_range() {
        let mut b = Body::new(0.0);
        b.on_event(&Event::new("sim.body").with("temp", 119.0).with("battery", 3.0).with("charging", false), 0.0);
        let out = b.tick(99999.0);
        let d = out.iter().find(|e| e.topic == "body.drives").unwrap();
        for key in ["energy", "arousal", "social", "comfort"] {
            let v = d.f64(key).unwrap();
            assert!((0.0..=1.0).contains(&v), "{key} = {v}");
        }
    }

    #[test]
    fn panel_recovery_does_not_claim_the_lid_is_open() {
        let mut b = Body::physical(0.0);
        assert!(b.lid_blocked());
        assert!(b.snapshot().data["battery"].is_null());
        b.recover_panel();
        let recovery_seq = b.lid_seq();
        assert!(b.lid_blocked() && b.lid_event().bool("show_face") == Some(true));
        b.set_lid(Some(true));
        b.panel_ready(recovery_seq);
        assert!(!b.panel_ready);
        b.set_lid(Some(false));
        b.panel_ready(b.lid_seq());
        assert!(b.panel_ready && !b.lid_blocked());
    }

    #[test]
    fn a_full_battery_on_ac_is_not_reporting_a_charge() {
        let mut b = Body::physical(0.0);
        b.set_power(Some(100.0), Some(false), Some(true));
        let line = b.senses_line();
        assert!(line.contains("battery 100% plugged in"), "{line}");
        assert!(!line.contains("room") && !line.contains("cool"), "{line}");
    }

    #[test]
    fn core_temperature_uses_the_hottest_channel_and_invalidates_on_sensor_failure() {
        let tree = Tree::new("coretemp");
        let root = tree.0.clone();
        let coretemp = root.join("hwmon0");
        tree.file("hwmon0/name", "coretemp\n");
        tree.file("hwmon0/temp1_input", "49900\n");
        tree.file("hwmon0/temp2_input", "65250\n");
        tree.file("hwmon1/name", "acpitz\n");
        tree.file("hwmon1/temp1_input", "90000\n");

        let mut body = Body::physical(0.0);
        let reading = sysfs::read_core_temperature(&root);
        assert_eq!(reading, Some(65.25));
        body.set_temperature(reading);
        assert_eq!(body.tick(0.1)[0].f64("temp"), Some(65.25));
        body.set_temperature(reading);
        assert!(body.tick(1.0).is_empty());

        fs::write(coretemp.join("temp2_input"), "invalid\n").unwrap();
        body.set_temperature(sysfs::read_core_temperature(&root));
        assert!(body.tick(1.1)[0].data["temp"].is_null());
        fs::remove_dir_all(&coretemp).unwrap();
        assert_eq!(sysfs::read_core_temperature(&root), None);
    }

    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::{Duration, Instant};
    use sysfs::{Reading, Sensors};

    /// A throwaway sysfs tree, removed when the test ends.
    struct Tree(PathBuf);

    impl Tree {
        fn new(tag: &str) -> Tree {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let n = NEXT.fetch_add(1, Ordering::SeqCst);
            let root = std::env::temp_dir().join(format!("being-sysfs-{}-{tag}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).unwrap();
            Tree(root)
        }

        fn file(&self, path: &str, data: impl AsRef<[u8]>) {
            let path = self.0.join(path);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, data).unwrap();
        }

        fn dir(&self, path: &str) {
            fs::create_dir_all(self.0.join(path)).unwrap();
        }

        fn remove(&self, path: &str) {
            let path = self.0.join(path);
            if path.is_dir() { fs::remove_dir_all(path).unwrap() } else { fs::remove_file(path).unwrap() }
        }

        fn root(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    /// The sysfs a MacBookPro9,2 shows under Linux 6.x, from public dumps
    /// of the ACPI battery, applesmc, i915 and ACPI button drivers.
    fn macbook_pro_9_2(tag: &str) -> Tree {
        let t = Tree::new(tag);
        t.file("sys/class/power_supply/ADP1/type", "Mains\n");
        t.file("sys/class/power_supply/ADP1/online", "1\n");
        t.file("sys/class/power_supply/BAT0/type", "Battery\n");
        t.file("sys/class/power_supply/BAT0/present", "1\n");
        t.file("sys/class/power_supply/BAT0/status", "Charging\n");
        t.file("sys/class/power_supply/BAT0/capacity", "87\n");
        t.file("sys/class/power_supply/BAT0/energy_now", "52200000\n");
        t.file("sys/class/power_supply/BAT0/energy_full", "60000000\n");
        // A Bluetooth mouse's battery must not become the body's.
        t.file("sys/class/power_supply/hid-mouse-battery/type", "Battery\n");
        t.file("sys/class/power_supply/hid-mouse-battery/scope", "Device\n");
        t.file("sys/class/power_supply/hid-mouse-battery/capacity", "5\n");
        t.file("sys/class/hwmon/hwmon0/name", "acpitz\n");
        t.file("sys/class/hwmon/hwmon0/temp1_input", "99000\n");
        t.file("sys/class/hwmon/hwmon1/name", "applesmc\n");
        t.file("sys/class/hwmon/hwmon2/name", "coretemp\n");
        t.file("sys/class/hwmon/hwmon2/temp1_input", "52000\n");
        t.file("sys/class/hwmon/hwmon2/temp2_input", "50000\n");
        t.file("sys/class/hwmon/hwmon2/temp3_input", "51000\n");
        let smc = "sys/devices/platform/applesmc.768";
        t.file(&format!("{smc}/light"), "(3,0)\n");
        t.file(&format!("{smc}/temp1_label"), "TB0T\n");
        t.file(&format!("{smc}/temp1_input"), "31000\n");
        t.file(&format!("{smc}/temp2_label"), "TC0D\n");
        t.file(&format!("{smc}/temp2_input"), "56250\n");
        t.file(&format!("{smc}/temp3_label"), "TC0P\n");
        t.file(&format!("{smc}/temp3_input"), "-127000\n");
        t.file(&format!("{smc}/fan1_input"), "1999\n");
        t.file("sys/class/backlight/acpi_video0/type", "firmware\n");
        t.file("sys/class/backlight/acpi_video0/bl_power", "0\n");
        t.file("sys/class/backlight/intel_backlight/type", "raw\n");
        t.file("sys/class/backlight/intel_backlight/brightness", "2000\n");
        t.file("sys/class/backlight/intel_backlight/max_brightness", "4882\n");
        t.file("sys/class/backlight/intel_backlight/bl_power", "0\n");
        t.file("sys/class/leds/smc::kbd_backlight/brightness", "0\n");
        t.file("sys/class/leds/smc::kbd_backlight/max_brightness", "255\n");
        t.file("sys/class/input/event0/device/name", "Lid Switch\n");
        t.file("sys/class/input/event0/device/capabilities/sw", "1\n");
        t.file("sys/class/input/event1/device/name", "Power Button\n");
        t.file("sys/class/input/event1/device/capabilities/sw", "0\n");
        t.file("sys/class/input/event4/device/name", "Apple Inc. Apple Internal Keyboard / Trackpad\n");
        t.file("sys/class/input/event4/device/capabilities/sw", "0\n");
        t.file("sys/class/input/mouse0/device/name", "not an event node\n");
        t
    }

    #[test]
    fn the_real_macbook_layout_is_read_and_surveyed() {
        let tree = macbook_pro_9_2("mbp");
        let mut sensors = Sensors::new(tree.0.clone());
        let reading = sensors.read(Instant::now());
        assert_eq!(
            reading,
            Reading {
                battery: Some(87.0),
                charging: Some(true),
                on_ac: Some(true),
                temp: Some(52.0),
                lux: Some(12.0),
                energy_wh: Some(52.2),
                power_w: None,
                voltage_v: None,
                health_pct: None,
                cycles: None,
                ..Reading::default()
            }
        );
        let notes = sensors.notes.take().join("\n");
        assert!(notes.contains("found battery BAT0") && notes.contains("found AC adapter ADP1"), "{notes}");
        assert!(notes.contains("ignoring hid-mouse-battery"), "{notes}");
        // Said once, not on every pass.
        sensors.read(Instant::now());
        assert!(sensors.notes.take().is_empty());

        let survey = sysfs::survey(tree.root()).join("\n");
        for want in [
            "lid: found \"Lid Switch\" (event0)",
            "input: event4 \"Apple Inc. Apple Internal Keyboard / Trackpad\"",
            "power: found battery BAT0",
            "backlight: intel_backlight (raw), brightness 2000/4882, bl_power 0",
            "keyboard light: smc::kbd_backlight, brightness 0/255",
            "smc: found applesmc.768, 3 temperature channels, 1 fans",
            "light sensor: found, raw 3",
            "temperature: hwmon acpitz, applesmc, coretemp",
        ] {
            assert!(survey.contains(want), "missing {want:?} in\n{survey}");
        }
        assert!(!survey.contains("mouse0"), "{survey}");
        let order: Vec<_> = sysfs::backlight_controls(tree.root()).into_iter().map(|(name, _, _)| name).collect();
        assert_eq!(order, ["intel_backlight", "acpi_video0"]);
    }

    #[test]
    fn battery_health_and_cycles_come_from_the_real_laptops_figures() {
        // The first real boot (23 November): 4,684 of 5,770 mAh, 71 cycles.
        let tree = macbook_pro_9_2("wear");
        let bat = "sys/class/power_supply/BAT0";
        tree.file(&format!("{bat}/charge_full"), "4684000\n");
        tree.file(&format!("{bat}/charge_full_design"), "5770000\n");
        tree.file(&format!("{bat}/cycle_count"), "71\n");
        let reading = Sensors::new(tree.0.clone()).read(Instant::now());
        assert_eq!((reading.health_pct, reading.cycles), (Some(81.0), Some(71)));
        let mut body = Body::physical(0.0);
        body.sense(&reading);
        let state = body.tick(0.0).into_iter().find(|e| e.topic == "body.state").unwrap();
        assert_eq!(state.f64("health_pct"), Some(81.0));
        assert_eq!(state.data["cycles"].as_i64(), Some(71));

        // Energy figures stand in for charge ones; nonsense is unknown.
        tree.remove(&format!("{bat}/charge_full"));
        tree.file(&format!("{bat}/energy_full_design"), "75000000\n");
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert_eq!(power.health_pct, Some(80.0), "60 of 75 Wh");
        tree.file(&format!("{bat}/energy_full_design"), "0\n");
        tree.file(&format!("{bat}/cycle_count"), "0\n");
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert_eq!((power.health_pct, power.cycles), (None, None), "0 cycles means not counted");
        tree.file(&format!("{bat}/energy_full_design"), "50000000\n");
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert_eq!(power.health_pct, Some(100.0), "never more than new");

        // The lab can pretend.
        let mut sim = Body::new(0.0);
        sim.on_event(&Event::new("sim.body").with("health_pct", 64.4).with("cycles", 900), 0.0);
        let state = sim.snapshot();
        assert_eq!((state.f64("health_pct"), state.data["cycles"].as_i64()), (Some(64.0), Some(900)));
    }

    #[test]
    fn a_machine_with_nothing_reads_unknown_and_says_so() {
        let tree = Tree::new("empty");
        let mut sensors = Sensors::new(tree.0.clone());
        assert_eq!(sensors.read(Instant::now()), Reading::default());
        let survey = sysfs::survey(tree.root()).join("\n");
        for want in ["no lid switch", "power: no power_supply", "backlight: none found", "keyboard light: not found", "applesmc not found", "no hwmon chips"] {
            assert!(survey.contains(want), "missing {want:?} in\n{survey}");
        }
        // A root that is not even a directory.
        let file = tree.0.join("plain");
        fs::write(&file, "x").unwrap();
        assert_eq!(Sensors::new(file.clone()).read(Instant::now()), Reading::default());
        assert!(!sysfs::survey(&file).is_empty());
    }

    #[test]
    fn garbage_values_are_unknown_and_a_missing_capacity_falls_back() {
        let tree = macbook_pro_9_2("garbage");
        let bat = "sys/class/power_supply/BAT0";
        tree.file(&format!("{bat}/capacity"), "eighty\n");
        tree.file(&format!("{bat}/status"), "Unknown\n");
        tree.file("sys/class/power_supply/ADP1/online", "2\n");
        tree.file("sys/devices/platform/applesmc.768/light", "(3;0)\n");
        let mut sensors = Sensors::new(tree.0.clone());
        let reading = sensors.read(Instant::now());
        assert_eq!(reading.battery, Some(87.0), "energy_now / energy_full");
        assert_eq!(reading.energy_wh, Some(52.2));
        assert_eq!((reading.charging, reading.on_ac, reading.lux), (None, None, None));

        tree.remove(&format!("{bat}/energy_now"));
        tree.file(&format!("{bat}/charge_now"), "6000\n");
        tree.file(&format!("{bat}/charge_full"), "5000\n");
        assert_eq!(sysfs::read_power(tree.root(), &mut sysfs::Notes::default()).battery, Some(100.0), "worn battery clamps");
        tree.file(&format!("{bat}/charge_full"), "0\n");
        tree.file(&format!("{bat}/capacity"), "250\n");
        assert_eq!(sysfs::read_power(tree.root(), &mut sysfs::Notes::default()).battery, None);

        for bad in ["", "()", "(1)", "(-1,2)", "(99999,0)", "3,0", "(1,2,3)"] {
            assert_eq!(sysfs::parse_light(bad), None, "{bad:?}");
        }
        assert_eq!(sysfs::parse_light(" (0, 7)\n"), Some(7));

        // Not text, huge, and a negative temperature.
        tree.file("sys/class/hwmon/hwmon2/temp2_input", [0xff, 0xfe, 0x00]);
        assert_eq!(sysfs::read_core_temperature(&tree.0.join("sys/class/hwmon")), None);
        tree.file("sys/class/hwmon/hwmon2/temp2_input", "1".repeat(100_000));
        assert_eq!(sysfs::read_core_temperature(&tree.0.join("sys/class/hwmon")), None);
        tree.file("sys/class/hwmon/hwmon2/temp2_input", "-5000");
        assert_eq!(sysfs::read_core_temperature(&tree.0.join("sys/class/hwmon")), None);
    }

    #[test]
    fn unreadable_files_behave_like_eio() {
        // A directory where a value should be fails every read, as EIO does.
        let tree = macbook_pro_9_2("eio");
        for path in [
            "sys/class/power_supply/BAT0/capacity",
            "sys/class/power_supply/BAT0/energy_now",
            "sys/class/power_supply/BAT0/status",
            "sys/class/power_supply/ADP1/type",
            "sys/devices/platform/applesmc.768/light",
            "sys/class/hwmon/hwmon2/temp1_input",
            "sys/class/input/event0/device/capabilities/sw",
        ] {
            tree.remove(path);
            tree.dir(path);
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink("/nonexistent/being", tree.0.join("sys/class/power_supply/BAT1")).unwrap();
        let mut sensors = Sensors::new(tree.0.clone());
        let reading = sensors.read(Instant::now());
        assert_eq!(reading, Reading::default());
        let notes = sensors.notes.take().join("\n");
        assert!(notes.contains("ignoring ADP1, its type is unreadable"), "{notes}");
        assert!(notes.contains("battery BAT0 reports no readable charge"), "{notes}");
        let survey = sysfs::survey(tree.root()).join("\n");
        assert!(survey.contains("no lid switch") && survey.contains("light sensor: not found or not answering"), "{survey}");
    }

    #[test]
    fn the_smc_stands_in_for_coretemp_and_its_junk_keys_are_skipped() {
        let tree = macbook_pro_9_2("smc");
        tree.remove("sys/class/hwmon/hwmon2");
        let smc = sysfs::find_smc(tree.root());
        assert_eq!(sysfs::read_temperature(tree.root(), smc.as_deref()), Some(56.25));
        // A coretemp chip with no channels does not hide the SMC.
        tree.file("sys/class/hwmon/hwmon9/name", "coretemp\n");
        assert_eq!(sysfs::read_temperature(tree.root(), smc.as_deref()), Some(56.25));
        // Found through the platform bus when the device path differs.
        tree.remove("sys/devices/platform/applesmc.768");
        tree.file("sys/bus/platform/devices/applesmc.769/light", "(0,0)\n");
        let smc = sysfs::find_smc(tree.root()).unwrap();
        assert_eq!(sysfs::read_light(&smc), Some(0));
        assert_eq!(sysfs::read_temperature(tree.root(), Some(&smc)), None);
    }

    #[test]
    fn a_short_failure_keeps_the_last_reading_and_a_long_one_does_not() {
        let tree = macbook_pro_9_2("hold");
        let mut sensors = Sensors::new(tree.0.clone());
        let start = Instant::now();
        let good = sensors.read(start);
        tree.remove("sys/class/power_supply");
        tree.remove("sys/devices/platform/applesmc.768");
        tree.remove("sys/class/hwmon");
        assert_eq!(sensors.read(start + Duration::from_secs(3)), good);
        assert_eq!(sensors.read(start + Duration::from_secs(11)), Reading::default());
        // And it does not come back from the dead.
        assert_eq!(sensors.read(start + Duration::from_secs(12)), Reading::default());
    }

    #[test]
    fn a_full_worn_pack_reads_full_and_its_health_stays_separate() {
        // Measured on the laptop, 25 November 2026 (ACPI Smart Battery):
        // capacity is against the design, so the old reading said 80%.
        let tree = macbook_pro_9_2("sbs-full");
        let bat = "sys/class/power_supply/BAT0";
        tree.remove(&format!("{bat}/energy_now"));
        tree.remove(&format!("{bat}/energy_full"));
        for (name, value) in [
            ("status", "Full"),
            ("capacity", "80"),
            ("charge_now", "4593000"),
            ("charge_full", "4629000"),
            ("charge_full_design", "5770000"),
            ("voltage_now", "12431000"),
            ("current_now", "0"),
            ("cycle_count", "71"),
        ] {
            tree.file(&format!("{bat}/{name}"), format!("{value}\n"));
        }
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert_eq!((power.battery, power.health_pct, power.cycles), (Some(100.0), Some(80.0), Some(71)));
        assert_eq!(power.charging, Some(false));
        let mut body = Body::physical(0.0);
        body.sense(&Sensors::new(tree.0.clone()).read(Instant::now()));
        assert!(body.senses_line().contains("battery 100% plugged in"), "{}", body.senses_line());
        // Still charging at that charge: 99, the honest figure.
        tree.file(&format!("{bat}/status"), "Charging\n");
        assert_eq!(sysfs::read_power(tree.root(), &mut sysfs::Notes::default()).battery, Some(99.0));
        // Half of today's full is 50%, though it is 40% of the design.
        tree.file(&format!("{bat}/status"), "Discharging\n");
        tree.file(&format!("{bat}/charge_now"), "2314500\n");
        tree.file(&format!("{bat}/capacity"), "40\n");
        assert_eq!(sysfs::battery_percent(&tree.0.join(bat)), Some(50.0));
        // No pairs at all: capacity is what there is.
        tree.remove(&format!("{bat}/charge_now"));
        assert_eq!(sysfs::battery_percent(&tree.0.join(bat)), Some(40.0));
    }

    #[test]
    fn the_battery_is_read_slowly_and_at_once_when_the_adapter_changes() {
        let tree = macbook_pro_9_2("poll");
        let bat = "sys/class/power_supply/BAT0";
        let mut sensors = Sensors::new(tree.0.clone());
        let start = Instant::now();
        let at = |s: u64| start + Duration::from_secs(s);
        assert_eq!(sensors.read(at(0)).battery, Some(87.0));
        // The sensor thread passes every 2 s: the charge is not re-read...
        tree.file(&format!("{bat}/energy_now"), "48000000\n");
        for s in (2..60).step_by(2) {
            assert_eq!(sensors.read(at(s)).battery, Some(87.0), "{s} s");
        }
        assert_eq!(sensors.power.reads, 1);
        // ...until a minute has passed on AC.
        assert_eq!(sensors.read(at(60)).battery, Some(80.0));
        assert_eq!(sensors.power.reads, 2);
        // The temperature and the light still follow every pass.
        tree.file("sys/class/hwmon/hwmon2/temp1_input", "70000\n");
        assert_eq!(sensors.read(at(62)).temp, Some(70.0));
        // Pulled out of the wall: read at once, once more 4 s on (the gauge
        // can lag the adapter), then every 20 s.
        tree.file("sys/class/power_supply/ADP1/online", "0\n");
        tree.file(&format!("{bat}/status"), "Discharging\n");
        let r = sensors.read(at(63));
        assert_eq!((r.on_ac, r.charging), (Some(false), Some(false)));
        assert_eq!(sensors.power.reads, 3);
        sensors.read(at(65));
        assert_eq!(sensors.power.reads, 3);
        sensors.read(at(67));
        assert_eq!(sensors.power.reads, 4, "the follow-up");
        tree.file(&format!("{bat}/energy_now"), "12000000\n");
        assert_eq!(sensors.read(at(85)).battery, Some(80.0));
        assert_eq!(sensors.read(at(87)).battery, Some(20.0), "20 s on battery");
        // Low: every 10 s, for the pleas and the power-off.
        tree.file(&format!("{bat}/energy_now"), "6000000\n");
        assert_eq!(sensors.read(at(91)).battery, Some(20.0));
        assert_eq!(sensors.read(at(96)).battery, Some(20.0));
        assert_eq!(sensors.read(at(97)).battery, Some(10.0));
        // Plugged in again: at once.
        tree.file("sys/class/power_supply/ADP1/online", "1\n");
        tree.file(&format!("{bat}/status"), "Charging\n");
        assert_eq!(sensors.read(at(98)).charging, Some(true));
        // A pack whose directory goes is unknown at once, not a minute later.
        tree.remove(bat);
        let r = sensors.read(at(100));
        assert_eq!((r.battery, r.on_ac), (Some(10.0), Some(true)), "held as any short failure is");
        assert_eq!(sensors.read(at(109)).battery, None);
        let reads = sensors.power.reads;
        for s in 110..200 {
            sensors.read(at(s));
        }
        assert!(sensors.power.reads - reads <= 2, "no battery listed: nothing to read");
    }

    #[test]
    fn a_status_that_lags_the_adapter_is_corrected_seconds_later() {
        // Unplugged while charging: ADP1/online says 0 at once, but the
        // first read of the gauge still says Charging. Before the follow-up
        // read the senses said "charging" while unplugged for 20 s.
        let tree = macbook_pro_9_2("poll-lag");
        let bat = "sys/class/power_supply/BAT0";
        tree.file(&format!("{bat}/status"), "Charging\n");
        let mut sensors = Sensors::new(tree.0.clone());
        let start = Instant::now();
        let at = |s: u64| start + Duration::from_secs(s);
        sensors.read(at(0));
        tree.file("sys/class/power_supply/ADP1/online", "0\n");
        let r = sensors.read(at(10));
        assert_eq!((r.on_ac, r.charging), (Some(false), Some(true)), "the stale edge read");
        tree.file(&format!("{bat}/status"), "Discharging\n");
        assert_eq!(sensors.read(at(12)).charging, Some(true));
        assert_eq!(sensors.read(at(14)).charging, Some(false), "fixed 4 s on");
        assert_eq!(sensors.power.reads, 3);
        // One follow-up only: then the 20 s cadence.
        for s in (16..34).step_by(2) {
            sensors.read(at(s));
        }
        assert_eq!(sensors.power.reads, 3);
        assert_eq!(sensors.read(at(34)).charging, Some(false));
        assert_eq!(sensors.power.reads, 4);
    }

    #[test]
    fn a_pack_that_goes_while_bat0_stays_listed_is_seen_from_the_uevent() {
        // The sbs driver keeps BAT0 and flips present to 0; the adapter does
        // not change. 60-being-power.rules touches the stamp on the uevent.
        let tree = macbook_pro_9_2("poll-uevent");
        let bat = "sys/class/power_supply/BAT0";
        let mut sensors = Sensors::new(tree.0.clone());
        let start = Instant::now();
        let at = |s: u64| start + Duration::from_secs(s);
        assert_eq!(sensors.read(at(0)).battery, Some(87.0));
        tree.file(&format!("{bat}/present"), "0\n");
        assert_eq!(sensors.read(at(2)).battery, Some(87.0), "no uevent yet: not seen");
        tree.file(sysfs::POWER_STAMP, "");
        sensors.read(at(4));
        assert_eq!(sensors.power.reads, 2, "read on the uevent");
        assert_eq!(sensors.read(at(15)).battery, None, "unknown once the hold is over, not a minute later");
        // One follow-up 4 s after the uevent read, then quiet.
        let reads = sensors.power.reads;
        for s in 16..60 {
            sensors.read(at(s));
        }
        assert_eq!(reads, 3);
        assert_eq!(sensors.power.reads, 3);
    }

    #[test]
    fn a_battery_that_reports_no_charge_is_tried_again_soon_but_not_every_pass() {
        let tree = macbook_pro_9_2("poll-retry");
        let bat = "sys/class/power_supply/BAT0";
        tree.remove(&format!("{bat}/energy_now"));
        tree.file(&format!("{bat}/capacity"), "busy\n");
        let mut sensors = Sensors::new(tree.0.clone());
        let start = Instant::now();
        assert_eq!(sensors.read(start).battery, None);
        tree.file(&format!("{bat}/capacity"), "64\n");
        assert_eq!(sensors.read(start + Duration::from_secs(4)).battery, None);
        assert_eq!(sensors.read(start + Duration::from_secs(10)).battery, Some(64.0));
        assert_eq!(sensors.power.reads, 2);
    }

    #[test]
    fn lid_capability_bitmaps() {
        for (bits, lid) in [("1", true), ("0", false), ("3", true), ("10 1", true), ("ffff 0", false), ("", false), ("zz", false)] {
            assert_eq!(sysfs::has_lid_switch(bits), lid, "{bits:?}");
        }
    }

    #[test]
    fn the_hub_feed_never_waits_and_goes_unknown_when_the_thread_does() {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        let mut feed = sysfs::Feed::new(rx, 0.0);
        let mut body = Body::physical(0.0);
        assert_eq!(feed.update(&mut body, 1.0), None);
        tx.send(Reading { battery: Some(50.0), on_ac: Some(false), lux: Some(0.0), ..Reading::default() }).unwrap();
        assert_eq!(feed.update(&mut body, 2.0), None);
        assert_eq!(body.snapshot().f64("battery"), Some(50.0));
        assert!(body.dark());
        assert_eq!(feed.update(&mut body, 20.0), None);
        assert!(feed.update(&mut body, 40.0).is_some_and(|line| line.contains("no reading for 30 s")));
        assert!(body.snapshot().data["battery"].is_null() && !body.dark());
        assert_eq!(feed.update(&mut body, 41.0), None, "said once");
        tx.send(Reading { battery: Some(49.0), ..Reading::default() }).unwrap();
        feed.update(&mut body, 42.0);
        assert_eq!(body.snapshot().f64("battery"), Some(49.0));
        drop(tx);
        assert!(feed.update(&mut body, 43.0).is_some_and(|line| line.contains("stopped")));
        assert!(body.snapshot().data["battery"].is_null());
    }

    #[test]
    fn energy_and_draw_come_from_energy_or_from_charge_and_current() {
        let tree = macbook_pro_9_2("energy");
        let bat = "sys/class/power_supply/BAT0";
        tree.file(&format!("{bat}/status"), "Discharging\n");
        tree.file("sys/class/power_supply/ADP1/online", "0\n");
        tree.file(&format!("{bat}/power_now"), "12500000\n");
        tree.file(&format!("{bat}/voltage_now"), "11400000\n");
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert_eq!((power.energy_wh, power.power_w, power.voltage_v), (Some(52.2), Some(12.5), Some(11.4)));
        // A battery that counts charge and signs its current.
        tree.remove(&format!("{bat}/energy_now"));
        tree.remove(&format!("{bat}/power_now"));
        tree.file(&format!("{bat}/charge_now"), "2000000\n");
        tree.file(&format!("{bat}/current_now"), "-1000000\n");
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert!((power.energy_wh.unwrap() - 22.8).abs() < 1e-9);
        assert!((power.power_w.unwrap() - 11.4).abs() < 1e-9);
        // Nonsense voltage: no derived figures.
        tree.file(&format!("{bat}/voltage_now"), "0\n");
        let power = sysfs::read_power(tree.root(), &mut sysfs::Notes::default());
        assert_eq!((power.energy_wh, power.power_w, power.voltage_v), (None, None, None));
    }

    #[test]
    fn the_body_publishes_its_battery_level_and_asks_for_the_power_off() {
        let mut b = Body::physical(0.0);
        b.set_power(Some(40.0), Some(false), Some(false));
        let state = b.tick(0.0).into_iter().find(|e| e.topic == "body.state").unwrap();
        assert_eq!(state.str("battery_level"), Some("ok"));
        assert!(state.data["minutes_left"].is_null());
        // The draw climbs and the energy runs down: a plea, then a goodbye.
        b.sense(&Reading { battery: Some(30.0), charging: Some(false), on_ac: Some(false), energy_wh: Some(5.0), power_w: Some(15.0), voltage_v: Some(10.9), ..Reading::default() });
        let mut out = Vec::new();
        for i in 1..=70 {
            out.extend(b.tick(f64::from(i)));
        }
        let pleas: Vec<_> = out.iter().filter(|e| e.topic == "body.battery").filter_map(|e| e.str("level")).collect();
        assert_eq!(pleas, ["low"], "20 minutes left at 30%, held for the confirmation time");
        let state = out.iter().rev().find(|e| e.topic == "body.state").unwrap();
        assert_eq!(state.bool("battery_low"), Some(true));
        assert_eq!(state.f64("minutes_left"), Some(20.0));
        assert!(b.senses_line().contains("battery 30% and low, unplugged, about 20 min left"), "{}", b.senses_line());
        b.sense(&Reading { battery: Some(4.0), charging: Some(false), on_ac: Some(false), ..Reading::default() });
        let mut out = Vec::new();
        for i in 71..=101 {
            out.extend(b.tick(f64::from(i)));
        }
        assert!(out.iter().any(|e| e.topic == "stem.shutdown"));
        assert!(!b.take_power_off());
        b.mind_saved();
        b.tick(102.0);
        assert!(b.take_power_off());
        assert!(!b.take_power_off(), "said once");
    }

    #[test]
    fn an_early_save_receipt_does_not_cut_a_later_grace_short() {
        let mut b = Body::physical(0.0);
        b.set_power(Some(40.0), Some(false), Some(false));
        b.tick(0.0);
        b.mind_saved(); // stray: no shutdown announced
        b.sense(&Reading { battery: Some(4.0), charging: Some(false), on_ac: Some(false), ..Reading::default() });
        assert!(!b.tick(1.0).iter().any(|e| e.topic == "stem.shutdown"), "one reading is not enough");
        assert!(b.tick(31.0).iter().any(|e| e.topic == "stem.shutdown"));
        b.tick(32.0);
        assert!(!b.take_power_off(), "the grace still runs");
        b.tick(52.0);
        assert!(b.take_power_off(), "then the power goes");
    }

    #[test]
    fn a_restarted_body_numbers_lid_readings_past_the_old_ones() {
        let mut b = Body::physical(0.0);
        b.continue_lid_seq(7);
        assert_eq!(b.lid_seq(), 8);
        b.set_lid(Some(false));
        assert_eq!((b.lid_seq(), b.lid()), (9, Some(false)));
        b.continue_lid_seq(3);
        assert_eq!(b.lid_seq(), 9, "never goes backwards");
    }
}

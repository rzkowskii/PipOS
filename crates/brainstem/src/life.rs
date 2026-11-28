//! The being's life record: when it was born, which body it is in, how many
//! times it has woken, and how its last time awake ended.
//!
//! It lives in the mind partition as one small file, always replaced whole, so
//! a power cut can never leave it half-written.

use serde_json::{json, Value};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const FILE: &str = "life.json";
const CLOCK_FLOOR: u64 = 1_767_225_600; // mind/clock_epoch.py
const MAX_CLOCK: u64 = 4_133_980_799;

pub fn saved_clock_floor(dir: &Path) -> u64 {
    let mut value = String::new();
    let result = File::open(dir.join("clock-floor")).and_then(|file| file.take(16).read_to_string(&mut value));
    if result.is_ok() {
        if let Ok(stamp) = value.trim().parse::<u64>() {
            if (CLOCK_FLOOR..=MAX_CLOCK).contains(&stamp) {
                return stamp;
            }
        }
    }
    CLOCK_FLOOR
}

pub fn wall_timestamp(floor: u64) -> u64 {
    let floor = floor.max(CLOCK_FLOOR);
    let now = unix_now();
    if now > MAX_CLOCK { floor } else { now.max(floor) }
}

/// At most one periodic disk write. A slow disk must not hold up the hub.
#[derive(Default)]
pub struct HeartbeatWrite(Option<JoinHandle<io::Result<()>>>);

impl HeartbeatWrite {
    pub fn start(&mut self, wake: &Life, now: u64) -> io::Result<()> {
        let wake = wake.clone();
        self.spawn(move || wake.mark_alive(now))
    }

    fn spawn(&mut self, write: impl FnOnce() -> io::Result<()> + Send + 'static) -> io::Result<()> {
        if self.0.is_none() {
            self.0 = Some(thread::Builder::new().name("life-heartbeat".into()).spawn(write)?);
        }
        Ok(())
    }

    pub fn completed(&mut self) -> Option<io::Result<()>> {
        if !self.0.as_ref().is_some_and(JoinHandle::is_finished) {
            return None;
        }
        self.0.take().map(|task| task.join().unwrap_or_else(|_| Err(io::Error::other("life heartbeat panicked"))))
    }

    /// The hub uses this only while exiting. An overdue write stays owned by
    /// this process until it exits, and the life lock still guards the stop.
    pub fn drain(&mut self, budget: Duration) -> bool {
        let deadline = Instant::now() + budget;
        while self.0.is_some() {
            if self.completed().is_some() {
                return true;
            }
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return false;
            }
            thread::sleep(left.min(Duration::from_millis(5)));
        }
        true
    }
}

/// What is true of this wake. Worked out once, when it wakes.
#[derive(Clone, Debug, PartialEq)]
pub struct Life {
    pub id: String,
    /// Unix seconds.
    pub born: u64,
    /// How many times it has woken, this one included.
    pub wakes: u64,
    pub woke_at: u64,
    /// This is the first time it has ever woken.
    pub first: bool,
    /// Last time, it was awake when the power went: it never got to sleep.
    pub lost_power: bool,
    /// Its memory has woken in different hardware from last time.
    pub new_body: bool,
    pub body: String,
    /// Seconds asleep before this wake, when it went to sleep properly.
    pub slept: Option<u64>,
    /// CLOCK_BOOTTIME at this wake, when known. "Awake N" is counted on it,
    /// so a wall clock set (or corrected) after waking cannot stretch or
    /// shrink how long it has been awake.
    pub woke_boot_clock: Option<f64>,
    dir: PathBuf,
    boot: String,
    writer: String,
}

fn bad(msg: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.into())
}

fn lock_record(dir: &Path) -> io::Result<File> {
    // Restore takes the same directory lock while publishing. A heartbeat
    // can retry later when the lock is busy instead of delaying the organs.
    let directory = File::open(dir)?;
    directory.try_lock().map_err(io::Error::from)?;
    if dir.join(".being-restore-in-progress").try_exists()? {
        return Err(bad("The life record is waiting for a restore to finish"));
    }
    Ok(directory)
}

fn start_writer(record: &mut Value, boot: &str, wakes: u64, writer: &str, now: u64) {
    record["liveness"] = json!({"boot": boot, "wakes": wakes, "writer": writer});
    record["alive_at"] = json!(now);
    record["ended"] = Value::Null;
}

fn liveness_matches_wake(record: &Value) -> bool {
    record["boot"].is_string() && record["wakes"].is_u64()
        && record["boot"] == record["liveness"]["boot"] && record["wakes"] == record["liveness"]["wakes"]
}

fn write_whole(dir: &Path, record: &Value) -> io::Result<()> {
    let tmp = dir.join(format!("{FILE}.tmp"));
    {
        let mut f = File::create(&tmp)?;
        f.write_all(record.to_string().as_bytes())?;
        f.sync_all()?;
    }
    fs::rename(&tmp, dir.join(FILE))?;
    // Make the rename itself durable.
    File::open(dir)?.sync_all()
}

fn read(dir: &Path) -> io::Result<Option<Value>> {
    match fs::read_to_string(dir.join(FILE)) {
        Ok(text) => {
            let value: Value = serde_json::from_str(&text).map_err(|e| bad(format!("{FILE} is damaged: {e}")))?;
            if value.get("id").and_then(Value::as_str).is_none() || value.get("born").and_then(Value::as_u64).is_none() {
                return Err(bad(format!("{FILE} has no identity in it")));
            }
            Ok(Some(value))
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

fn new_id() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    io::Read::read_exact(&mut File::open("/dev/urandom")?, &mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

impl Life {
    /// Wake. `boot` identifies this boot of the machine, and `body` describes
    /// the hardware. Restarting the brainstem within one boot is not a new wake.
    #[cfg(test)]
    pub fn wake(dir: &Path, now: u64, boot: &str, body: &str) -> io::Result<Life> {
        Life::wake_at(dir, now, None, boot, body)
    }

    /// Wake, with the boot clock read now (`boot_clock`, seconds since the
    /// machine started, sleep included).
    pub fn wake_at(dir: &Path, now: u64, boot_clock: Option<f64>, boot: &str, body: &str) -> io::Result<Life> {
        let _lock = lock_record(dir)?;
        let record = read(dir)?;
        let first = record.is_none();
        let mut record = match record {
            Some(r) => r,
            None => json!({ "id": new_id()?, "born": now, "wakes": 0, "body": body }),
        };
        let id = record["id"].as_str().unwrap_or_default().to_string();
        let born = record["born"].as_u64().unwrap_or(now);
        let was_awake = record.get("awake_since").is_some_and(|v| !v.is_null());
        let writer = new_id()?;

        if !first && was_awake && record.get("boot").and_then(Value::as_str) == Some(boot) {
            // Same boot: only the brainstem restarted. The facts of this wake stand.
            let wakes = record["wakes"].as_u64().unwrap_or(1);
            let woke_at = record["awake_since"].as_u64().unwrap_or(now);
            record["wakes"] = json!(wakes);
            record["awake_since"] = json!(woke_at);
            start_writer(&mut record, boot, wakes, &writer, now);
            write_whole(dir, &record)?;
            let this = &record["this_wake"];
            // The boot clock of this wake was written when it began. A record
            // without one (an older brainstem) counts from the wall clock.
            let woke_boot_clock = record.get("awake_since_boot_clock").and_then(Value::as_f64);
            return Ok(Life {
                woke_boot_clock,
                id,
                born,
                wakes,
                woke_at,
                first: this["first"].as_bool().unwrap_or(false),
                lost_power: this["lost_power"].as_bool().unwrap_or(false),
                new_body: this["new_body"].as_bool().unwrap_or(false),
                body: body.to_string(),
                slept: this["slept"].as_u64(),
                dir: dir.to_path_buf(),
                boot: boot.to_string(),
                writer,
            });
        }

        let lost_power = was_awake;
        let new_body = !first && record.get("body").and_then(Value::as_str) != Some(body);
        let slept = if lost_power {
            None
        } else {
            record.get("last_sleep").and_then(Value::as_u64).map(|t| now.saturating_sub(t))
        };
        let wakes = record["wakes"].as_u64().unwrap_or(0) + 1;
        // Old slots preserve unknown keys in life.json. Count a heartbeat as
        // evidence only when it belongs to the wake we just found on disk.
        let previous_alive_at = if liveness_matches_wake(&record) { record["alive_at"].as_u64() } else { None };
        let previous_ended = if was_awake { Some("abrupt") } else if slept.is_some() { Some("clean") } else { None };

        record["wakes"] = json!(wakes);
        record["body"] = json!(body);
        record["boot"] = json!(boot);
        record["awake_since"] = json!(now);
        record["awake_since_boot_clock"] = json!(boot_clock);
        start_writer(&mut record, boot, wakes, &writer, now);
        record["this_wake"] = json!({ "first": first, "lost_power": lost_power, "new_body": new_body, "slept": slept,
                                      "previous_ended": previous_ended, "previous_alive_at": previous_alive_at });
        write_whole(dir, &record)?;

        Ok(Life {
            id,
            born,
            wakes,
            woke_at: now,
            first,
            lost_power,
            new_body,
            body: body.to_string(),
            slept,
            woke_boot_clock: boot_clock,
            dir: dir.to_path_buf(),
            boot: boot.to_string(),
            writer,
        })
    }

    fn active_record(&self) -> io::Result<Value> {
        let record = read(&self.dir)?.ok_or_else(|| bad(format!("{FILE} has gone")))?;
        if record["id"].as_str() != Some(self.id.as_str()) || record["born"].as_u64() != Some(self.born)
            || record["boot"].as_str() != Some(self.boot.as_str()) || record["wakes"].as_u64() != Some(self.wakes)
            || record["awake_since"].as_u64() != Some(self.woke_at)
            || !liveness_matches_wake(&record)
            || record["liveness"]["writer"].as_str() != Some(self.writer.as_str())
        {
            return Err(bad("The life record belongs to a different wake or writer"));
        }
        Ok(record)
    }

    pub fn mark_alive(&self, now: u64) -> io::Result<()> {
        let _lock = lock_record(&self.dir)?;
        let mut record = self.active_record()?;
        record["alive_at"] = json!(now);
        write_whole(&self.dir, &record)
    }

    /// Go to sleep properly. After this, the next wake knows it was not a power cut.
    pub fn sleep(&self, now: u64) -> io::Result<()> {
        let _lock = lock_record(&self.dir)?;
        let mut record = self.active_record()?;
        record["awake_since"] = Value::Null;
        record["awake_since_boot_clock"] = Value::Null;
        record["last_sleep"] = json!(now);
        record["alive_at"] = json!(now);
        record["ended"] = json!("clean");
        write_whole(&self.dir, &record)
    }

    /// For the bus: everything an organ may want to know about this wake.
    pub fn event(&self) -> bus::Event {
        let mut ev = bus::Event::new("life.woke")
            .with("id", self.id.as_str())
            .with("born", self.born)
            .with("wakes", self.wakes)
            .with("woke_at", self.woke_at)
            .with("first", self.first)
            .with("lost_power", self.lost_power)
            .with("new_body", self.new_body)
            .with("body", self.body.as_str());
        if let Some(slept) = self.slept {
            ev = ev.with("slept", slept);
        }
        ev
    }

    /// A few true words about itself, for the senses line.
    #[cfg(test)]
    pub fn senses(&self, now: u64) -> String {
        self.senses_at(now, None, true)
    }

    /// The same, counting time awake on the boot clock when both ends of it
    /// are known.
    pub fn senses_at(&self, now: u64, boot_clock: Option<f64>, trusted_clock: bool) -> String {
        let awake = match (boot_clock, self.woke_boot_clock) {
            (Some(clock), Some(woke)) if clock >= woke => (clock - woke) as u64,
            _ => now.saturating_sub(self.woke_at),
        };
        let mut parts = vec![format!("awake {}", span(awake))];
        // What happened on the way here is worth knowing for a while, not forever.
        if awake < 15 * 60 {
            if self.first {
                parts.push("this is your first time awake".to_string());
            }
            if self.lost_power {
                parts.push("last time you lost power while awake".to_string());
            }
            if self.new_body {
                parts.push("you are in a different body from last time".to_string());
            }
        }
        let days = now.saturating_sub(self.born) / 86_400;
        if !self.first && trusted_clock {
            parts.push(match days {
                0 => "born today".to_string(),
                1 => "1 day old".to_string(),
                n => format!("{n} days old"),
            });
        }
        parts.join(", ")
    }
}

fn span(seconds: u64) -> String {
    match seconds {
        s if s < 90 => "1 min".to_string(),
        s if s < 3600 => format!("{} min", s / 60),
        s if s < 2 * 3600 => "1 hour".to_string(),
        s => format!("{} hours", s / 3600),
    }
}

/// An identifier for this boot of the machine.
pub fn boot_id() -> String {
    if let Ok(id) = std::env::var("BEING_BOOT_ID") {
        return id;
    }
    if let Ok(id) = fs::read_to_string("/proc/sys/kernel/random/boot_id") {
        return id.trim().to_string();
    }
    // Elsewhere (a development Mac): every start counts as a new wake.
    format!("start-{}-{}", std::process::id(), unix_now())
}

/// A description of the hardware it is running in.
pub fn body() -> String {
    if let Ok(body) = std::env::var("BEING_BODY") {
        return body;
    }
    let dmi = |name: &str| fs::read_to_string(format!("/sys/class/dmi/id/{name}")).map(|s| s.trim().to_string()).unwrap_or_default();
    let (vendor, product) = (dmi("sys_vendor"), dmi("product_name"));
    let cpu = fs::read_to_string("/proc/cpuinfo")
        .ok()
        .and_then(|text| {
            text.lines().find(|l| l.starts_with("model name")).and_then(|l| l.split(':').nth(1)).map(|s| s.trim().to_string())
        })
        .unwrap_or_default();
    let parts: Vec<&str> = [vendor.as_str(), product.as_str(), cpu.as_str()].into_iter().filter(|s| !s.is_empty()).collect();
    if parts.is_empty() {
        "an unknown machine".to_string()
    } else {
        parts.join(" / ")
    }
}

/// Seconds on CLOCK_BOOTTIME: since the machine started, counting suspend,
/// and never set by anyone. None where there is no such clock.
pub fn boot_clock() -> Option<f64> {
    #[cfg(target_os = "linux")]
    {
        // SAFETY: an all-zero timespec is valid (some C libraries pad it),
        // and clock_gettime writes only into it.
        let mut ts: libc::timespec = unsafe { std::mem::zeroed() };
        let status = unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut ts) };
        if status == 0 {
            return Some(ts.tv_sec as f64 + ts.tv_nsec as f64 / 1e9);
        }
        None
    }
    #[cfg(not(target_os = "linux"))]
    {
        None
    }
}

pub fn unix_now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("being-life-{}-{tag}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    const T0: u64 = 1_800_000_000;

    #[test]
    fn an_incomplete_restore_prevents_a_new_wake() {
        let d = dir("restore-in-progress");
        fs::write(d.join(".being-restore-in-progress"), "pending").unwrap();
        assert!(Life::wake(&d, T0, "boot-1", "lab").is_err());
        assert!(!d.join(FILE).exists());
        fs::remove_file(d.join(".being-restore-in-progress")).unwrap();
        assert!(Life::wake(&d, T0, "boot-1", "lab").unwrap().first);
    }

    #[test]
    fn the_first_wake_is_a_birth() {
        let d = dir("birth");
        let life = Life::wake(&d, T0, "boot-1", "body-a").unwrap();
        assert!(life.first && !life.lost_power && !life.new_body);
        assert_eq!((life.wakes, life.born, life.slept), (1, T0, None));
        assert_eq!(life.id.len(), 32);
        assert!(life.senses(T0 + 30).contains("first time awake"));
    }

    #[test]
    fn a_proper_sleep_then_wake_keeps_its_identity_and_counts() {
        let d = dir("sleep");
        let a = Life::wake(&d, T0, "boot-1", "body-a").unwrap();
        a.sleep(T0 + 600).unwrap();
        let b = Life::wake(&d, T0 + 8 * 3600, "boot-2", "body-a").unwrap();
        assert_eq!((b.id.clone(), b.born), (a.id, T0), "same being, same birthday");
        assert_eq!(b.wakes, 2);
        assert!(!b.first && !b.lost_power && !b.new_body);
        assert_eq!(b.slept, Some(8 * 3600 - 600));
        assert_eq!(b.senses(T0 + 8 * 3600 + 200), "awake 3 min, born today");
    }

    #[test]
    fn a_wake_without_a_sleep_means_it_lost_power() {
        let d = dir("cut");
        Life::wake(&d, T0, "boot-1", "body-a").unwrap();
        // No sleep: the plug was pulled.
        let b = Life::wake(&d, T0 + 100, "boot-2", "body-a").unwrap();
        assert!(b.lost_power);
        assert_eq!(b.slept, None);
        assert!(b.senses(T0 + 160).contains("last time you lost power while awake"));
        // It stops mentioning it after a while.
        assert!(!b.senses(T0 + 100 + 3600).contains("lost power"));
        // And once it sleeps properly, the next wake is clean again.
        b.sleep(T0 + 200).unwrap();
        assert!(!Life::wake(&d, T0 + 300, "boot-3", "body-a").unwrap().lost_power);
    }

    #[test]
    fn restarting_the_brainstem_is_not_a_new_wake() {
        let d = dir("restart");
        Life::wake(&d, T0, "boot-1", "body-a").unwrap().sleep(T0 + 10).unwrap();
        let a = Life::wake(&d, T0 + 100, "boot-2", "body-a").unwrap();
        let b = Life::wake(&d, T0 + 150, "boot-2", "body-a").unwrap();
        assert_eq!(b.wakes, a.wakes);
        assert_eq!(b.woke_at, T0 + 100);
        assert!(!b.lost_power, "a crashed brainstem is not a power cut");
        assert_eq!(b.slept, a.slept);
    }

    #[test]
    fn a_blocked_heartbeat_never_blocks_polling_or_starts_a_second_writer() {
        let (started, running) = std::sync::mpsc::channel();
        let (release, blocked) = std::sync::mpsc::channel();
        let mut heartbeat = HeartbeatWrite::default();
        heartbeat.spawn(move || {
            started.send(()).unwrap();
            blocked.recv_timeout(Duration::from_secs(5)).unwrap();
            Ok(())
        }).unwrap();
        running.recv_timeout(Duration::from_secs(2)).unwrap();
        let original = heartbeat.0.as_ref().unwrap().thread().id();
        assert!(heartbeat.completed().is_none());
        assert!(!heartbeat.drain(Duration::ZERO));
        heartbeat.spawn(|| panic!("the worker is already busy")).unwrap();
        assert_eq!(heartbeat.0.as_ref().unwrap().thread().id(), original);
        release.send(()).unwrap();
        assert!(heartbeat.drain(Duration::from_secs(2)));
        assert!(heartbeat.0.is_none());
    }

    #[test]
    fn a_delayed_heartbeat_cannot_overwrite_a_restarted_or_closed_wake() {
        let d = dir("heartbeat-after-close");
        let wake = Life::wake(&d, T0, "boot", "body").unwrap();
        let (release, blocked) = std::sync::mpsc::channel();
        let mut heartbeat = HeartbeatWrite::default();
        heartbeat.spawn(move || {
            blocked.recv_timeout(Duration::from_secs(5)).unwrap();
            wake.mark_alive(T0 + 300)
        }).unwrap();

        let resumed = Life::wake(&d, T0 + 320, "boot", "body").unwrap();
        resumed.sleep(T0 + 400).unwrap();
        release.send(()).unwrap();
        assert!(heartbeat.drain(Duration::from_secs(2)));
        let record = read(&d).unwrap().unwrap();
        assert_eq!(record["alive_at"], T0 + 400);
        assert_eq!(record["ended"], "clean");
    }

    #[test]
    fn heartbeat_and_end_evidence_follow_the_wake_and_its_current_writer() {
        let d = dir("liveness");
        let first = Life::wake(&d, T0, "boot-1", "body").unwrap();
        first.mark_alive(T0 + 300).unwrap();
        let current = Life::wake(&d, T0 + 400, "boot-1", "body").unwrap();
        assert_eq!(current.wakes, first.wakes);
        assert_eq!(current.woke_at, first.woke_at);
        assert!(first.mark_alive(T0 + 500).is_err());
        assert!(first.sleep(T0 + 500).is_err());
        assert_eq!(read(&d).unwrap().unwrap()["alive_at"], T0 + 400);
        current.mark_alive(T0 + 700).unwrap();

        let next = Life::wake(&d, T0 + 1000, "boot-2", "body").unwrap();
        assert!(next.lost_power);
        let record = read(&d).unwrap().unwrap();
        assert_eq!(record["this_wake"]["previous_ended"], "abrupt");
        assert_eq!(record["this_wake"]["previous_alive_at"], T0 + 700);
        assert!(record["ended"].is_null());
        assert!(current.mark_alive(T0 + 1050).is_err());
        let resumed = Life::wake(&d, T0 + 1080, "boot-2", "body").unwrap();
        assert_eq!(resumed.wakes, next.wakes);
        let after_restart = read(&d).unwrap().unwrap();
        assert_eq!(after_restart["this_wake"], record["this_wake"]);
        assert!(next.sleep(T0 + 1100).is_err());
        resumed.sleep(T0 + 1100).unwrap();
        assert_eq!(read(&d).unwrap().unwrap()["ended"], "clean");
        assert!(resumed.mark_alive(T0 + 1200).is_err());

        Life::wake(&d, T0 + 1300, "boot-3", "body").unwrap();
        let record = read(&d).unwrap().unwrap();
        assert_eq!(record["this_wake"]["previous_ended"], "clean");
        assert_eq!(record["this_wake"]["previous_alive_at"], T0 + 1100);
        assert_eq!(record["alive_at"], T0 + 1300);
    }

    #[test]
    fn restore_locks_and_identity_changes_keep_stale_actions_from_the_record() {
        let d = dir("liveness-restore");
        let wake = Life::wake(&d, T0, "boot-1", "body").unwrap();
        let path = d.join(FILE);
        let before = fs::read(&path).unwrap();
        let directory = File::open(&d).unwrap();
        directory.try_lock().unwrap();
        assert_eq!(wake.mark_alive(T0 + 300).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert_eq!(wake.sleep(T0 + 300).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        drop(directory);
        let marker = d.join(".being-restore-in-progress");
        fs::write(&marker, "pending").unwrap();
        assert!(wake.mark_alive(T0 + 300).is_err());
        assert!(wake.sleep(T0 + 300).is_err());
        assert_eq!(fs::read(&path).unwrap(), before);
        fs::remove_file(marker).unwrap();

        let mut replacement = read(&d).unwrap().unwrap();
        replacement["id"] = json!("bad0bad0bad0bad0bad0bad0bad0bad0");
        write_whole(&d, &replacement).unwrap();
        assert!(wake.mark_alive(T0 + 300).is_err());
        assert!(wake.sleep(T0 + 300).is_err());
        assert_eq!(read(&d).unwrap().unwrap(), replacement);
    }

    #[test]
    fn old_slots_do_not_lend_stale_heartbeat_evidence_to_a_new_wake() {
        let d = dir("liveness-rollback");
        let initial = Life::wake(&d, T0, "boot-1", "body").unwrap();
        initial.sleep(T0 + 300).unwrap();
        let mut old_slot = read(&d).unwrap().unwrap();
        old_slot["boot"] = json!("boot-2");
        old_slot["wakes"] = json!(2);
        old_slot["awake_since"] = json!(T0 + 500);
        write_whole(&d, &old_slot).unwrap();

        let current = Life::wake(&d, T0 + 900, "boot-3", "body").unwrap();
        assert_eq!(current.wakes, 3);
        let record = read(&d).unwrap().unwrap();
        assert_eq!(record["this_wake"]["previous_ended"], "abrupt");
        assert!(record["this_wake"]["previous_alive_at"].is_null());
        assert_eq!(record["alive_at"], T0 + 900);
    }

    #[test]
    fn waking_in_other_hardware_is_noticed_once() {
        let d = dir("body");
        Life::wake(&d, T0, "boot-1", "QEMU / lab").unwrap().sleep(T0 + 10).unwrap();
        let moved = Life::wake(&d, T0 + 100, "boot-2", "Apple Inc. / MacBookPro9,2").unwrap();
        assert!(moved.new_body);
        assert!(moved.senses(T0 + 130).contains("different body"));
        moved.sleep(T0 + 200).unwrap();
        assert!(!Life::wake(&d, T0 + 300, "boot-3", "Apple Inc. / MacBookPro9,2").unwrap().new_body);
    }

    #[test]
    fn a_damaged_record_is_an_error_not_a_rebirth() {
        let d = dir("damaged");
        let a = Life::wake(&d, T0, "boot-1", "body-a").unwrap();
        fs::write(d.join(FILE), "{ not json").unwrap();
        assert!(Life::wake(&d, T0 + 10, "boot-2", "body-a").is_err());
        fs::write(d.join(FILE), "{\"wakes\": 3}").unwrap();
        assert!(Life::wake(&d, T0 + 10, "boot-2", "body-a").is_err());
        // A leftover temporary file from an interrupted write is harmless.
        fs::write(d.join(FILE), json!({"id": a.id, "born": T0, "wakes": 1, "body": "body-a"}).to_string()).unwrap();
        fs::write(d.join(format!("{FILE}.tmp")), "half").unwrap();
        assert_eq!(Life::wake(&d, T0 + 10, "boot-2", "body-a").unwrap().id, a.id);
    }

    #[test]
    fn age_is_told_in_days() {
        let d = dir("age");
        Life::wake(&d, T0, "boot-1", "b").unwrap().sleep(T0 + 5).unwrap();
        let later = Life::wake(&d, T0 + 12 * 86_400 + 50, "boot-2", "b").unwrap();
        assert!(later.senses(T0 + 12 * 86_400 + 5 * 3600).ends_with("awake 4 hours, 12 days old"));
    }

    #[test]
    fn awake_time_follows_the_boot_clock_across_a_restart_and_a_clock_jump() {
        let d = dir("boot-clock");
        Life::wake_at(&d, T0, Some(5.0), "boot-1", "b").unwrap().sleep(T0 + 5).unwrap();
        let a = Life::wake_at(&d, T0 + 100, Some(12.0), "boot-2", "b").unwrap();
        // The wall clock is corrected by a day; the boot clock moved 10 minutes.
        assert!(a.senses_at(T0 + 100 + 86_400, Some(612.0), true).starts_with("awake 10 min"));
        // The brainstem restarts within the boot: the same wake, the same clock.
        let b = Life::wake_at(&d, T0 + 90_000, Some(700.0), "boot-2", "b").unwrap();
        assert_eq!(b.woke_boot_clock, Some(12.0));
        assert!(b.senses_at(T0 + 90_000, Some(3612.0), true).starts_with("awake 1 hour"));
        // Without a boot clock it falls back to the wall clock.
        assert!(b.senses_at(T0 + 100 + 300, None, true).starts_with("awake 5 min"));
        assert!(b.senses_at(T0 + 100 + 300, Some(1.0), true).starts_with("awake 5 min"), "a clock that went back is not trusted");
    }
}

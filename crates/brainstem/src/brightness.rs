//! The panel's brightness, for "brighter", "dimmer", "set your brightness
//! to 50 percent" and the brightness keys (topic `being.brightness`, from the
//! mind only).
//!
//! Only the brightness file is written, never `bl_power` (that is the lid's
//! blanking, in linux_body). The native GPU control (`intel_backlight` on the
//! MacBookPro9,2, 0-1808 on the real laptop) is preferred over firmware ones.
//! The level is held between a floor that still shows the eyes and full,
//! and the brightness found at start is put back when the brainstem stops,
//! so a change by voice never outlives the being that made it.

use bus::Event;
use serde_json::Value;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// Never dimmer than this share of full: the face must stay visible.
pub const MIN_PCT: i64 = 5;
/// One "brighter" or "dimmer", in percentage points.
pub const STEP_PCT: i64 = 15;
/// The largest step a request may name with `by` (a key steps by 6, about a
/// sixteenth of full, as a Mac does).
pub const MAX_BY_PCT: i64 = 50;

/// The reply to a `being.*` request, carrying the request's `req` back when
/// it has a short tag (letters, digits, `_`, `-` or `.`, up to 32): the
/// mind's keys tag theirs, so a spoken "brighter" waiting on its own answer
/// is never answered by a key pressed at the same moment.
pub fn reply_to(topic: &str, ev: &Event) -> Event {
    let reply = Event::new(topic);
    match ev.str("req") {
        Some(tag)
            if !tag.is_empty()
                && tag.len() <= 32
                && tag.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.')) =>
        {
            reply.with("req", tag)
        }
        _ => reply,
    }
}

pub struct Brightness {
    /// The device directory and its maximum raw value.
    control: Option<(String, PathBuf, i64)>,
    /// The raw value found before the first change, put back on exit.
    original: Option<i64>,
}

fn read_number(path: &Path) -> Option<i64> {
    crate::body::sysfs::read_text(path)?.parse().ok()
}

impl Brightness {
    /// No panel to change (the lab and the simulator).
    pub fn none() -> Self {
        Self { control: None, original: None }
    }

    /// Find the panel's control under `root` (normally "/").
    pub fn discover(root: &Path) -> Self {
        let control = crate::body::sysfs::backlight_controls(root).into_iter().find_map(|(name, _kind, bl_power)| {
            let dir = bl_power.parent()?.to_path_buf();
            let max = read_number(&dir.join("max_brightness")).filter(|m| *m > 0 && *m < 1_000_000)?;
            read_number(&dir.join("brightness"))?;
            Some((name, dir, max))
        });
        Self { control, original: None }
    }

    pub fn name(&self) -> Option<&str> {
        self.control.as_ref().map(|(name, _, _)| name.as_str())
    }

    fn raw_to_pct(raw: i64, max: i64) -> i64 {
        ((raw as f64 * 100.0 / max as f64).round() as i64).clamp(0, 100)
    }

    fn pct_to_raw(pct: i64, max: i64) -> i64 {
        ((pct as f64 * max as f64 / 100.0).round() as i64).clamp(1, max)
    }

    /// The level now, percent of full.
    pub fn level(&self) -> Option<i64> {
        let (_, dir, max) = self.control.as_ref()?;
        Some(Self::raw_to_pct(read_number(&dir.join("brightness"))?, *max))
    }

    /// Apply a `being.brightness` request and say what happened, as a
    /// `body.brightness` event: the level it now has, whether it hit an end,
    /// or why nothing changed. A `step` of 1 or -1 moves by STEP_PCT, or by
    /// `by` points (1 to MAX_BY_PCT) when the request names it. A malformed
    /// request changes nothing.
    pub fn request(&mut self, ev: &Event) -> Event {
        let reply = reply_to("body.brightness", ev);
        let step = ev.data.get("step").and_then(Value::as_i64).filter(|s| *s == 1 || *s == -1);
        let level = ev.data.get("level").and_then(Value::as_f64).filter(|l| l.is_finite() && (0.0..=100.0).contains(l));
        let by = match ev.data.get("by") {
            None => Some(STEP_PCT),
            Some(by) => by.as_i64().filter(|by| (1..=MAX_BY_PCT).contains(by)),
        };
        if (step.is_none() && level.is_none()) || by.is_none() {
            return reply.with("ok", false).with("reason", "bad_request");
        }
        let by = by.unwrap_or(STEP_PCT);
        let Some((name, dir, max)) = self.control.clone() else {
            return reply.with("ok", false).with("reason", "no_control");
        };
        let path = dir.join("brightness");
        let Some(raw) = read_number(&path) else {
            return reply.with("ok", false).with("reason", "unreadable");
        };
        let now = Self::raw_to_pct(raw, max);
        let wanted = match (step, level) {
            (_, Some(level)) => level.round() as i64,
            (Some(step), None) => now + step * by,
            (None, None) => now,
        };
        let target = wanted.clamp(MIN_PCT, 100);
        let new_raw = Self::pct_to_raw(target, max);
        if new_raw != raw {
            self.original.get_or_insert(raw);
            if let Err(e) = fs::write(&path, new_raw.to_string()) {
                eprintln!("brainstem: brightness: could not write {name}: {e}");
                return reply.with("ok", false).with("reason", "write_failed").with("level", now);
            }
        }
        reply
            .with("ok", true)
            .with("level", target)
            .with("at_max", target >= 100)
            .with("at_min", target <= MIN_PCT)
            .with("limited", wanted != target)
    }

    /// Put back the brightness found before the first change.
    pub fn restore(&mut self) -> io::Result<()> {
        let (Some(raw), Some((_, dir, _))) = (self.original.take(), self.control.as_ref()) else { return Ok(()) };
        fs::write(dir.join("brightness"), raw.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Tree(PathBuf);

    impl Tree {
        fn new(tag: &str) -> Tree {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let n = NEXT.fetch_add(1, Ordering::SeqCst);
            let root = std::env::temp_dir().join(format!("being-bright-{}-{tag}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).unwrap();
            Tree(root)
        }

        fn file(&self, path: &str, data: &str) {
            let path = self.0.join(path);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, data).unwrap();
        }

        fn get(&self, path: &str) -> i64 {
            fs::read_to_string(self.0.join(path)).unwrap().trim().parse().unwrap()
        }
    }

    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    const INTEL: &str = "sys/class/backlight/intel_backlight";

    /// The real laptop on 23 November: intel_backlight 1707 of 1808, and a
    /// firmware duplicate that must not be the one changed.
    fn laptop(tag: &str) -> Tree {
        let t = Tree::new(tag);
        t.file("sys/class/backlight/acpi_video0/type", "firmware\n");
        t.file("sys/class/backlight/acpi_video0/brightness", "7\n");
        t.file("sys/class/backlight/acpi_video0/max_brightness", "15\n");
        t.file("sys/class/backlight/acpi_video0/bl_power", "0\n");
        t.file(&format!("{INTEL}/type"), "raw\n");
        t.file(&format!("{INTEL}/brightness"), "1707\n");
        t.file(&format!("{INTEL}/max_brightness"), "1808\n");
        t.file(&format!("{INTEL}/bl_power"), "0\n");
        t
    }

    fn step(n: i64) -> Event {
        Event::new("being.brightness").with("step", n)
    }

    #[test]
    fn steps_and_levels_change_the_native_control_within_bounds() {
        let t = laptop("steps");
        let mut b = Brightness::discover(&t.0);
        assert_eq!(b.name(), Some("intel_backlight"));
        assert_eq!(b.level(), Some(94));
        let out = b.request(&step(1));
        assert_eq!((out.bool("ok"), out.data["level"].as_i64(), out.bool("at_max")), (Some(true), Some(100), Some(true)));
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 1808);
        assert_eq!(out.bool("limited"), Some(true), "94 + 15 is past full");
        let out = b.request(&step(-1));
        assert_eq!(out.data["level"].as_i64(), Some(85));
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 1537);
        let out = b.request(&Event::new("being.brightness").with("level", 50));
        assert_eq!(out.data["level"].as_i64(), Some(50));
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 904);
        // Never dark: the face must still show.
        let out = b.request(&Event::new("being.brightness").with("level", 0));
        assert_eq!((out.data["level"].as_i64(), out.bool("at_min")), (Some(MIN_PCT), Some(true)));
        assert!(t.get(&format!("{INTEL}/brightness")) > 0);
        assert_eq!(t.get("sys/class/backlight/acpi_video0/brightness"), 7, "the firmware duplicate is left");
        assert_eq!(t.get(&format!("{INTEL}/bl_power")), 0, "never the lid's blanking");
        // Put back on exit.
        b.restore().unwrap();
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 1707);
        b.restore().unwrap();
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 1707, "once");
    }

    #[test]
    fn a_key_steps_by_the_points_it_names() {
        let t = laptop("by");
        let mut b = Brightness::discover(&t.0);
        let key = |n: i64| step(n).with("by", 6);
        assert_eq!(b.request(&key(-1)).data["level"].as_i64(), Some(88));
        assert_eq!(b.request(&key(1)).data["level"].as_i64(), Some(94));
        assert_eq!(b.request(&key(1)).data["level"].as_i64(), Some(100));
        b.request(&Event::new("being.brightness").with("level", 8));
        let out = b.request(&key(-1));
        assert_eq!((out.data["level"].as_i64(), out.bool("at_min")), (Some(MIN_PCT), Some(true)), "never dark");
        for by in [Value::from(0), Value::from(51), Value::from(-6), Value::from("six")] {
            let out = b.request(&step(1).with("by", by.clone()));
            assert_eq!(out.str("reason"), Some("bad_request"), "{by:?}");
        }
    }

    #[test]
    fn a_tagged_request_gets_its_tag_back() {
        let t = laptop("req");
        let mut b = Brightness::discover(&t.0);
        let out = b.request(&step(1).with("by", 6).with("req", "key"));
        assert_eq!((out.str("req"), out.bool("ok")), (Some("key"), Some(true)));
        assert_eq!(b.request(&step(1)).str("req"), None, "untagged stays untagged");
        let bad = b.request(&Event::new("being.brightness").with("req", "key"));
        assert_eq!((bad.str("req"), bad.str("reason")), (Some("key"), Some("bad_request")), "a refusal too");
        let long = "k".repeat(33);
        for tag in [Value::from(""), Value::from(long.as_str()), Value::from("a b"), Value::from(7)] {
            assert_eq!(b.request(&step(-1).with("req", tag.clone())).str("req"), None, "{tag:?}");
        }
        let none = Brightness::discover(&t.0.join("nowhere")).request(&step(1).with("req", "key"));
        assert_eq!((none.str("req"), none.str("reason")), (Some("key"), Some("no_control")));
    }

    #[test]
    fn bad_requests_and_missing_panels_change_nothing() {
        let t = laptop("bad");
        let mut b = Brightness::discover(&t.0);
        for ev in [
            step(2),
            step(0),
            Event::new("being.brightness").with("level", 140),
            Event::new("being.brightness").with("level", -3),
            Event::new("being.brightness").with("level", "bright"),
            Event::new("being.brightness"),
        ] {
            let out = b.request(&ev);
            assert_eq!((out.bool("ok"), out.str("reason")), (Some(false), Some("bad_request")), "{ev:?}");
        }
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 1707);
        b.restore().unwrap();
        assert_eq!(t.get(&format!("{INTEL}/brightness")), 1707, "nothing to put back");

        let empty = Tree::new("none");
        let mut none = Brightness::discover(&empty.0);
        assert_eq!(none.request(&step(1)).str("reason"), Some("no_control"));
        assert_eq!(Brightness::none().request(&step(-1)).str("reason"), Some("no_control"));
        assert!(none.restore().is_ok());
    }
}

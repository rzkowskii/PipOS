//! The keyboard's backlight, for the keyboard-light keys (F5 and F6 on the
//! MacBookPro9,2) through the mind (topic `being.kbd_light`, from the mind only).
//!
//! The control is the applesmc LED `smc::kbd_backlight` (0-255 on the real
//! laptop), found by name under /sys/class/leds; any LED whose name ends in
//! `kbd_backlight` stands in when that one is missing. A key steps it in
//! sixteenths, as a Mac does, and a toggle turns it off and back on to the
//! level it had. While the panel is dark (the lid closed) the light is off,
//! and it comes back with the panel. The level found at start is put back
//! when the brainstem stops, as the panel's brightness is.

use bus::Event;
use serde_json::Value;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// Steps from off to full: a Mac's keyboard light has sixteen.
pub const STEPS: i64 = 16;
/// The level a toggle turns the light on to when it has never been on.
pub const DEFAULT_PCT: i64 = 50;
/// The LED's name on Apple laptops (applesmc).
pub const SMC_NAME: &str = "smc::kbd_backlight";

pub struct KbdLight {
    /// The LED's name, its directory and its maximum raw value.
    control: Option<(String, PathBuf, i64)>,
    /// The raw value found before the first change, put back on exit.
    original: Option<i64>,
    /// The raw level a toggle (or the panel lighting again) turns back on to.
    lit: Option<i64>,
    /// The light was turned off because the panel went dark.
    darkened: bool,
}

fn read_number(path: &Path) -> Option<i64> {
    crate::body::sysfs::read_text(path)?.parse().ok()
}

impl KbdLight {
    /// No keyboard light to change (the lab and the simulator).
    pub fn none() -> Self {
        Self { control: None, original: None, lit: None, darkened: false }
    }

    /// Find the keyboard's LED under `root` (normally "/").
    pub fn discover(root: &Path) -> Self {
        let dir = root.join("sys/class/leds");
        let mut found: Vec<(String, PathBuf)> = fs::read_dir(&dir)
            .map(|entries| {
                entries
                    .flatten()
                    .filter_map(|entry| {
                        let name = entry.file_name().to_str()?.to_string();
                        name.ends_with("kbd_backlight").then(|| (name, entry.path()))
                    })
                    .collect()
            })
            .unwrap_or_default();
        // applesmc's own LED first, then the others in a fixed order.
        found.sort_by(|a, b| (a.0 != SMC_NAME, &a.0).cmp(&(b.0 != SMC_NAME, &b.0)));
        let control = found.into_iter().find_map(|(name, dir)| {
            let max = read_number(&dir.join("max_brightness")).filter(|m| *m > 0 && *m < 1_000_000)?;
            read_number(&dir.join("brightness"))?;
            Some((name, dir, max))
        });
        Self { control, ..Self::none() }
    }

    pub fn name(&self) -> Option<&str> {
        self.control.as_ref().map(|(name, _, _)| name.as_str())
    }

    fn raw_to_step(raw: i64, max: i64) -> i64 {
        ((raw as f64 * STEPS as f64 / max as f64).round() as i64).clamp(0, STEPS)
    }

    fn step_to_raw(step: i64, max: i64) -> i64 {
        ((step.clamp(0, STEPS) as f64 * max as f64 / STEPS as f64).round() as i64).clamp(0, max)
    }

    fn raw_to_pct(raw: i64, max: i64) -> i64 {
        ((raw as f64 * 100.0 / max as f64).round() as i64).clamp(0, 100)
    }

    fn pct_to_raw(pct: i64, max: i64) -> i64 {
        ((pct.clamp(0, 100) as f64 * max as f64 / 100.0).round() as i64).clamp(0, max)
    }

    /// The level now, percent of full.
    pub fn level(&self) -> Option<i64> {
        let (_, dir, max) = self.control.as_ref()?;
        Some(Self::raw_to_pct(read_number(&dir.join("brightness"))?, *max))
    }

    fn write(&mut self, dir: &Path, raw: i64, new_raw: i64) -> io::Result<()> {
        if new_raw != raw {
            self.original.get_or_insert(raw);
            fs::write(dir.join("brightness"), new_raw.to_string())?;
        }
        Ok(())
    }

    /// Apply a `being.kbd_light` request (`step` of 1 or -1, `level` from 0
    /// to 100, or `toggle: true`) and say what happened, as a
    /// `body.kbd_light` event: the level it now has and whether it hit an
    /// end, or why nothing changed. A malformed request changes nothing.
    pub fn request(&mut self, ev: &Event) -> Event {
        let reply = crate::brightness::reply_to("body.kbd_light", ev);
        let step = ev.data.get("step").and_then(Value::as_i64).filter(|s| *s == 1 || *s == -1);
        let level = ev.data.get("level").and_then(Value::as_f64).filter(|l| l.is_finite() && (0.0..=100.0).contains(l));
        let toggle = ev.bool("toggle") == Some(true);
        if [step.is_some(), level.is_some(), toggle].iter().filter(|b| **b).count() != 1 {
            return reply.with("ok", false).with("reason", "bad_request");
        }
        let Some((name, dir, max)) = self.control.clone() else {
            return reply.with("ok", false).with("reason", "no_control");
        };
        let Some(raw) = read_number(&dir.join("brightness")) else {
            return reply.with("ok", false).with("reason", "unreadable");
        };
        // A key while the panel was dark wins over the dark: the light is the
        // owner's again from here.
        let was_darkened = std::mem::take(&mut self.darkened);
        let base = if was_darkened { self.lit.unwrap_or(raw) } else { raw };
        let (new_raw, limited) = match (step, level) {
            (Some(step), _) => {
                let now = Self::raw_to_step(base, max);
                let wanted = now + step;
                (Self::step_to_raw(wanted, max), !(0..=STEPS).contains(&wanted))
            }
            (None, Some(level)) => (Self::pct_to_raw(level.round() as i64, max), false),
            (None, None) if base > 0 => (0, false),
            (None, None) => {
                let back = self.lit.filter(|lit| *lit > 0).unwrap_or_else(|| Self::pct_to_raw(DEFAULT_PCT, max));
                (back, false)
            }
        };
        if base > 0 && new_raw == 0 {
            // Remember the level a toggle brings back.
            self.lit = Some(base);
        } else if new_raw > 0 {
            self.lit = Some(new_raw);
        }
        if let Err(e) = self.write(&dir, raw, new_raw) {
            eprintln!("brainstem: keyboard light: could not write {name}: {e}");
            return reply.with("ok", false).with("reason", "write_failed").with("level", Self::raw_to_pct(raw, max));
        }
        let pct = Self::raw_to_pct(new_raw, max);
        reply
            .with("ok", true)
            .with("level", pct)
            .with("on", new_raw > 0)
            .with("at_max", new_raw >= max)
            .with("at_min", new_raw == 0)
            .with("limited", limited)
    }

    /// The panel went dark (`true`) or lit again (`false`). Dark turns a lit
    /// keyboard off; lit puts back what dark turned off. Nothing else is
    /// touched, so a light the owner turned off stays off.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn panel(&mut self, dark: bool) -> io::Result<()> {
        let Some((_, dir, _)) = self.control.clone() else { return Ok(()) };
        let Some(raw) = read_number(&dir.join("brightness")) else { return Ok(()) };
        if dark {
            if raw > 0 && !self.darkened {
                self.lit = Some(raw);
                self.darkened = true;
                self.write(&dir, raw, 0)?;
            }
        } else if std::mem::take(&mut self.darkened) {
            if let Some(lit) = self.lit.filter(|lit| *lit > 0) {
                self.write(&dir, raw, lit)?;
            }
        }
        Ok(())
    }

    /// Put back the level found before the first change.
    pub fn restore(&mut self) -> io::Result<()> {
        self.darkened = false;
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
            let root = std::env::temp_dir().join(format!("being-kbd-{}-{tag}-{n}", std::process::id()));
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

    const SMC: &str = "sys/class/leds/smc::kbd_backlight/brightness";

    /// The real laptop on 24 November: smc::kbd_backlight 0 of 255, beside
    /// LEDs that are not the keyboard's.
    fn laptop(tag: &str, now: i64) -> Tree {
        let t = Tree::new(tag);
        t.file("sys/class/leds/input3::capslock/brightness", "0\n");
        t.file("sys/class/leds/input3::capslock/max_brightness", "1\n");
        t.file("sys/class/leds/phy0-led/brightness", "1\n");
        t.file("sys/class/leds/phy0-led/max_brightness", "1\n");
        t.file(SMC, &format!("{now}\n"));
        t.file("sys/class/leds/smc::kbd_backlight/max_brightness", "255\n");
        t
    }

    fn step(n: i64) -> Event {
        Event::new("being.kbd_light").with("step", n)
    }

    fn toggle() -> Event {
        Event::new("being.kbd_light").with("toggle", true)
    }

    #[test]
    fn the_applesmc_light_steps_in_sixteenths_between_off_and_full() {
        let t = laptop("steps", 0);
        let mut k = KbdLight::discover(&t.0);
        assert_eq!(k.name(), Some(SMC_NAME));
        assert_eq!(k.level(), Some(0));
        let out = k.request(&step(-1));
        assert_eq!((out.bool("ok"), out.data["level"].as_i64(), out.bool("at_min")), (Some(true), Some(0), Some(true)));
        assert_eq!(out.bool("limited"), Some(true), "already off");
        let out = k.request(&step(1));
        assert_eq!((out.data["level"].as_i64(), out.bool("on")), (Some(6), Some(true)));
        assert_eq!(t.get(SMC), 16, "one sixteenth of 255");
        for _ in 0..20 {
            k.request(&step(1));
        }
        assert_eq!(t.get(SMC), 255);
        let out = k.request(&step(1));
        assert_eq!((out.data["level"].as_i64(), out.bool("at_max"), out.bool("limited")), (Some(100), Some(true), Some(true)));
        let out = k.request(&Event::new("being.kbd_light").with("level", 50));
        assert_eq!(out.data["level"].as_i64(), Some(50));
        assert_eq!(t.get(SMC), 128);
        assert_eq!(t.get("sys/class/leds/phy0-led/brightness"), 1, "other LEDs are left alone");
        k.restore().unwrap();
        assert_eq!(t.get(SMC), 0, "put back on exit");
        k.restore().unwrap();
        assert_eq!(t.get(SMC), 0, "once");
    }

    #[test]
    fn a_toggle_turns_it_off_and_back_to_the_level_it_had() {
        let t = laptop("toggle", 0);
        let mut k = KbdLight::discover(&t.0);
        let out = k.request(&toggle());
        assert_eq!((out.data["level"].as_i64(), out.bool("on")), (Some(DEFAULT_PCT), Some(true)), "never lit: half");
        k.request(&step(1));
        let lit = t.get(SMC);
        let out = k.request(&toggle());
        assert_eq!((out.data["level"].as_i64(), out.bool("on")), (Some(0), Some(false)));
        assert_eq!(t.get(SMC), 0);
        k.request(&toggle());
        assert_eq!(t.get(SMC), lit, "back to the level it had");
        // Stepping down to off and toggling brings back the last lit level.
        let t2 = laptop("toggle-step", 16);
        let mut k2 = KbdLight::discover(&t2.0);
        k2.request(&step(-1));
        assert_eq!(t2.get(SMC), 0);
        k2.request(&toggle());
        assert_eq!(t2.get(SMC), 16);
    }

    #[test]
    fn a_dark_panel_turns_the_light_off_and_a_lit_one_brings_it_back() {
        let t = laptop("panel", 96);
        let mut k = KbdLight::discover(&t.0);
        k.panel(true).unwrap();
        assert_eq!(t.get(SMC), 0, "off with the lid closed");
        k.panel(true).unwrap();
        k.panel(false).unwrap();
        assert_eq!(t.get(SMC), 96);
        k.panel(false).unwrap();
        assert_eq!(t.get(SMC), 96);
        // A light the owner turned off stays off when the panel lights.
        k.request(&toggle());
        k.panel(true).unwrap();
        k.panel(false).unwrap();
        assert_eq!(t.get(SMC), 0);
        // A key pressed while dark starts from the level dark turned off.
        k.request(&toggle());
        assert_eq!(t.get(SMC), 96);
        k.panel(true).unwrap();
        let out = k.request(&step(1));
        assert_eq!(t.get(SMC), 112, "96 is six sixteenths; one more is seven");
        assert_eq!(out.bool("on"), Some(true));
        k.panel(false).unwrap();
        assert_eq!(t.get(SMC), 112, "the key's level stands");
        k.restore().unwrap();
        assert_eq!(t.get(SMC), 96);
    }

    #[test]
    fn bad_requests_and_missing_lights_change_nothing() {
        let t = laptop("bad", 32);
        let mut k = KbdLight::discover(&t.0);
        for ev in [
            step(2),
            step(0),
            Event::new("being.kbd_light").with("level", 140),
            Event::new("being.kbd_light").with("level", -1),
            Event::new("being.kbd_light").with("level", "dim"),
            Event::new("being.kbd_light").with("toggle", false),
            Event::new("being.kbd_light").with("step", 1).with("toggle", true),
            Event::new("being.kbd_light"),
        ] {
            let out = k.request(&ev);
            assert_eq!((out.bool("ok"), out.str("reason")), (Some(false), Some("bad_request")), "{ev:?}");
        }
        assert_eq!(t.get(SMC), 32);
        k.restore().unwrap();
        assert_eq!(t.get(SMC), 32, "nothing to put back");
        // A key's tag comes back on its answer, as on body.brightness.
        assert_eq!(k.request(&step(1).with("req", "key")).str("req"), Some("key"));
        assert_eq!(k.request(&step(-1)).str("req"), None);

        let empty = Tree::new("none");
        let mut none = KbdLight::discover(&empty.0);
        assert_eq!(none.request(&step(1)).str("reason"), Some("no_control"));
        assert!(none.panel(true).is_ok() && none.restore().is_ok());
        assert_eq!(KbdLight::none().request(&toggle()).str("reason"), Some("no_control"));

        // Another keyboard's LED stands in when applesmc has none; a broken
        // one (no maximum) is passed over.
        let other = Tree::new("other");
        other.file("sys/class/leds/aaa::kbd_backlight/brightness", "1\n");
        other.file("sys/class/leds/asus::kbd_backlight/brightness", "1\n");
        other.file("sys/class/leds/asus::kbd_backlight/max_brightness", "3\n");
        let k = KbdLight::discover(&other.0);
        assert_eq!(k.name(), Some("asus::kbd_backlight"));
        assert_eq!(k.level(), Some(33));
    }
}

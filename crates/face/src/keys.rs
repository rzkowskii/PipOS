//! The keyboard's top row, as printed on a Mac.
//!
//! The laptop boots with `hid_apple.fnmode=1`, so the MacBookPro9,2's top row
//! sends what is printed on it (hid-apple's `apple_fn_keys` table) and fn+key
//! sends F1 to F12:
//!
//! | key | Linux keycode            | SDL2 (2.32, evdev)        |
//! |-----|--------------------------|---------------------------|
//! | F1  | KEY_BRIGHTNESSDOWN (224) | SDL_SCANCODE_BRIGHTNESSDOWN |
//! | F2  | KEY_BRIGHTNESSUP (225)   | SDL_SCANCODE_BRIGHTNESSUP |
//! | F3  | KEY_SCALE (120)          | none: SDL drops it        |
//! | F4  | KEY_DASHBOARD (204)      | none: SDL drops it        |
//! | F5  | KEY_KBDILLUMDOWN (229)   | SDL_SCANCODE_KBDILLUMDOWN |
//! | F6  | KEY_KBDILLUMUP (230)     | SDL_SCANCODE_KBDILLUMUP   |
//! | F7  | KEY_PREVIOUSSONG (165)   | SDL_SCANCODE_AUDIOPREV    |
//! | F8  | KEY_PLAYPAUSE (164)      | SDL_SCANCODE_AUDIOPLAY    |
//! | F9  | KEY_NEXTSONG (163)       | SDL_SCANCODE_AUDIONEXT    |
//! | F10 | KEY_MUTE (113)           | SDL_SCANCODE_MUTE         |
//! | F11 | KEY_VOLUMEDOWN (114)     | SDL_SCANCODE_VOLUMEDOWN   |
//! | F12 | KEY_VOLUMEUP (115)       | SDL_SCANCODE_VOLUMEUP     |
//! | ⏏   | KEY_EJECTCD (161)        | SDL_SCANCODE_EJECT        |
//!
//! SDL's Linux table maps KEY_SCALE (Mission Control) and KEY_DASHBOARD
//! (Launchpad, the same code as KEY_ALL_APPLICATIONS) to no scancode, and its
//! evdev reader drops keys with none, so `watch` reads those two straight
//! from the keyboards.
//!
//! The keys are matched by scancode, never by keycode: SDL may not name a
//! keycode for every scancode, and a scancode is what the key is.

// Mission Control and Launchpad are read from evdev, which only Linux has.
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use bus::Event;
use sdl2::keyboard::Scancode;
use std::time::{Duration, Instant};

/// A key on the top row (or any keyboard's media keys) with a meaning here.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Media {
    BrightnessDown,
    BrightnessUp,
    /// Mission Control (F3): the menu or setup, as F2 opens it.
    MissionControl,
    /// Launchpad (F4): the workshop terminal.
    Launchpad,
    KbdLightDown,
    KbdLightUp,
    KbdLightToggle,
    Previous,
    PlayPause,
    Next,
    Mute,
    VolumeDown,
    VolumeUp,
    Eject,
}

/// Linux keycodes SDL does not deliver.
pub const KEY_SCALE: u16 = 120;
pub const KEY_DASHBOARD: u16 = 204;

impl Media {
    /// The key behind an SDL scancode, if it is one of these.
    pub fn from_scancode(code: Scancode) -> Option<Media> {
        Some(match code {
            Scancode::BrightnessDown => Media::BrightnessDown,
            Scancode::BrightnessUp => Media::BrightnessUp,
            Scancode::KbdIllumDown => Media::KbdLightDown,
            Scancode::KbdIllumUp => Media::KbdLightUp,
            Scancode::KbdIllumToggle => Media::KbdLightToggle,
            Scancode::AudioPrev => Media::Previous,
            Scancode::AudioPlay => Media::PlayPause,
            Scancode::AudioNext => Media::Next,
            Scancode::Mute | Scancode::AudioMute => Media::Mute,
            Scancode::VolumeDown => Media::VolumeDown,
            Scancode::VolumeUp => Media::VolumeUp,
            Scancode::Eject => Media::Eject,
            _ => return None,
        })
    }

    /// The key behind a Linux keycode that SDL drops.
    pub fn from_evdev(code: u16) -> Option<Media> {
        match code {
            KEY_SCALE => Some(Media::MissionControl),
            KEY_DASHBOARD => Some(Media::Launchpad),
            _ => None,
        }
    }

    /// Held down, the key keeps stepping (as a Mac's level keys do).
    pub fn repeats(self) -> bool {
        matches!(
            self,
            Media::BrightnessDown | Media::BrightnessUp | Media::KbdLightDown | Media::KbdLightUp | Media::VolumeDown | Media::VolumeUp
        )
    }

    pub fn name(self) -> &'static str {
        match self {
            Media::BrightnessDown => "brightness down",
            Media::BrightnessUp => "brightness up",
            Media::MissionControl => "Mission Control",
            Media::Launchpad => "Launchpad",
            Media::KbdLightDown => "keyboard light down",
            Media::KbdLightUp => "keyboard light up",
            Media::KbdLightToggle => "keyboard light",
            Media::Previous => "previous track",
            Media::PlayPause => "play/pause",
            Media::Next => "next track",
            Media::Mute => "mute",
            Media::VolumeDown => "volume down",
            Media::VolumeUp => "volume up",
            Media::Eject => "eject",
        }
    }
}

/// What the face does with a key.
#[derive(Debug, PartialEq)]
pub enum Action {
    /// Tell the brainstem (and through it the mind).
    Send(Event),
    /// Open the menu or setup, as F2 does.
    Menu,
    /// Nothing to do: say so in the log, never type it anywhere.
    Nothing(&'static str),
}

/// The action for a key. `resting`: the eyes rest and no app panel has the
/// keyboard, so play/pause asks for attention (as Space does) rather than
/// stopping a reply (as Esc does).
pub fn action(key: Media, resting: bool) -> Action {
    let dir = |up: bool| if up { "up" } else { "down" };
    Action::Send(match key {
        Media::BrightnessDown | Media::BrightnessUp => {
            Event::new("key.brightness").with("dir", dir(key == Media::BrightnessUp))
        }
        Media::KbdLightDown | Media::KbdLightUp => Event::new("key.kbd_light").with("dir", dir(key == Media::KbdLightUp)),
        Media::KbdLightToggle => Event::new("key.kbd_light").with("dir", "toggle"),
        Media::VolumeDown | Media::VolumeUp => Event::new("key.volume").with("dir", dir(key == Media::VolumeUp)),
        Media::Mute => Event::new("key.mute_speaker"),
        Media::PlayPause if resting => Event::new("key.attend"),
        Media::PlayPause => Event::new("key.interrupt"),
        Media::Launchpad => Event::new("key.apps"),
        Media::MissionControl => return Action::Menu,
        Media::Previous | Media::Next => return Action::Nothing("there is no track to skip"),
        Media::Eject => return Action::Nothing("there is no disc to eject"),
    })
}

/// A held key steps at most this often: the kernel repeats about 30 times a
/// second, far faster than a level can be set and shown.
pub const REPEAT_EVERY: Duration = Duration::from_millis(110);

/// Which repeats of a held key go through.
#[derive(Default)]
pub struct Repeats {
    last: Option<(Media, Instant)>,
}

impl Repeats {
    /// Whether this press (`repeat`: a held key's repeat) should act.
    pub fn admit(&mut self, key: Media, repeat: bool, now: Instant) -> bool {
        if repeat {
            if !key.repeats() {
                return false;
            }
            if let Some((last, at)) = self.last {
                if last == key && now.saturating_duration_since(at) < REPEAT_EVERY {
                    return false;
                }
            }
        }
        self.last = Some((key, now));
        true
    }
}

/// How long the level bar stays up after the last change.
pub const LEVEL_SHOWN: Duration = Duration::from_millis(1500);
/// Segments in the bar: one per step of a Mac's keys.
pub const SEGMENTS: usize = 16;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Gauge {
    Brightness,
    Volume,
    KeyboardLight,
}

impl Gauge {
    fn label(self) -> &'static str {
        match self {
            Gauge::Brightness => "brightness",
            Gauge::Volume => "volume",
            Gauge::KeyboardLight => "keyboard light",
        }
    }
}

/// What stays on screen while the speaker is muted (the mute key, or the
/// volume keys stepped down to nothing): a muted Pip can still be seen to be
/// muted after the bar has gone, so nobody waits for an alarm it cannot ring.
pub const SPEAKER_MUTED: &str = "speaker muted";

/// The short bar that shows a new brightness, volume or keyboard light. It is
/// drawn from what the brainstem and the voice report they set, never from
/// the key, so it shows what really happened.
#[derive(Default)]
pub struct LevelBar {
    shown: Option<(Gauge, u8, Instant)>,
    changed: bool,
    /// The voice last reported level 0: only a key mutes it that far (by
    /// voice the mind never goes below its floor).
    speaker_muted: bool,
}

impl LevelBar {
    /// Take in a state event (`body.brightness`, `voice.volume_state`,
    /// `body.kbd_light`). A failed change shows nothing.
    pub fn event(&mut self, ev: &Event, now: Instant) {
        let gauge = match ev.topic.as_str() {
            "body.brightness" if ev.bool("ok") == Some(true) => Gauge::Brightness,
            "body.kbd_light" if ev.bool("ok") == Some(true) => Gauge::KeyboardLight,
            "voice.volume_state" => Gauge::Volume,
            _ => return,
        };
        let Some(level) = ev.f64("level").filter(|l| l.is_finite() && (0.0..=100.0).contains(l)) else { return };
        let level = level.round() as u8;
        if gauge == Gauge::Volume {
            self.speaker_muted = level == 0;
        }
        self.shown = Some((gauge, level, now));
        self.changed = true;
    }

    /// Whether the bar is up at `now`.
    pub fn visible(&self, now: Instant) -> bool {
        self.shown.is_some_and(|(_, _, at)| now.saturating_duration_since(at) < LEVEL_SHOWN)
    }

    /// Whether only the lasting "speaker muted" note is up (drawn fainter).
    pub fn only_muted_note(&self, now: Instant) -> bool {
        self.speaker_muted && !self.visible(now)
    }

    /// Whether the picture needs drawing again, clearing the mark.
    pub fn take_changed(&mut self) -> bool {
        std::mem::take(&mut self.changed)
    }

    /// The bar as one line of text, while it is up; after it, while the
    /// speaker is muted, the lasting SPEAKER_MUTED note.
    pub fn line(&self, now: Instant) -> Option<String> {
        if self.only_muted_note(now) {
            return Some(SPEAKER_MUTED.to_string());
        }
        let (gauge, level, _) = self.shown.filter(|_| self.visible(now))?;
        let filled = ((usize::from(level) * SEGMENTS) as f64 / 100.0).round() as usize;
        let filled = if level > 0 { filled.max(1) } else { 0 };
        let bar: String = (0..SEGMENTS).map(|i| if i < filled { '█' } else { '░' }).collect();
        let value = match (gauge, level) {
            (Gauge::Volume, 0) => "muted".to_string(),
            (Gauge::KeyboardLight, 0) => "off".to_string(),
            _ => format!("{level}%"),
        };
        Some(format!("{:<14} {bar} {value:>5}", gauge.label()))
    }
}

/// The capability bitmap in /sys/class/input/eventN/device/capabilities/key
/// (hex words, most significant first, each a C long): does it hold `code`?
pub fn has_key(caps: &str, code: u16, long_bits: usize) -> bool {
    let words: Vec<&str> = caps.split_whitespace().collect();
    let index = usize::from(code) / long_bits;
    let Some(word) = words.len().checked_sub(index + 1).and_then(|i| words.get(i)) else { return false };
    u64::from_str_radix(word, 16).is_ok_and(|w| w >> (usize::from(code) % long_bits) & 1 == 1)
}

/// Read Mission Control and Launchpad from the keyboards that have them, on
/// threads of their own that block on the device (no polling): a found key
/// goes to `tx`. SDL reads the same devices for every other key; evdev gives
/// each reader its own copy. (Built in host tests too, so it is checked on
/// a Mac; only Linux calls it.)
#[cfg(any(target_os = "linux", test))]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn watch(tx: std::sync::mpsc::Sender<Media>) {
    use std::collections::HashSet;
    use std::io::Read;
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex};

    let open: Arc<Mutex<HashSet<PathBuf>>> = Arc::default();
    let started = std::thread::Builder::new().name("face-keys".into()).spawn(move || loop {
        let long_bits = 8 * std::mem::size_of::<std::ffi::c_long>();
        if let Ok(entries) = std::fs::read_dir("/sys/class/input") {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().into_owned();
                if !name.starts_with("event") {
                    continue;
                }
                let caps = std::fs::read_to_string(entry.path().join("device/capabilities/key")).unwrap_or_default();
                if !(has_key(&caps, KEY_SCALE, long_bits) || has_key(&caps, KEY_DASHBOARD, long_bits)) {
                    continue;
                }
                let path = PathBuf::from("/dev/input").join(&name);
                if !open.lock().map(|mut set| set.insert(path.clone())).unwrap_or(false) {
                    continue;
                }
                let Ok(mut file) = std::fs::File::open(&path) else {
                    if let Ok(mut set) = open.lock() {
                        set.remove(&path);
                    }
                    continue;
                };
                eprintln!("face: reading Mission Control and Launchpad from {}", path.display());
                let (tx, open) = (tx.clone(), open.clone());
                let reader = std::thread::Builder::new().name("face-keys-dev".into()).spawn(move || {
                    // struct input_event: a struct timeval (two longs), then
                    // u16 type, u16 code and i32 value.
                    let long = std::mem::size_of::<std::ffi::c_long>();
                    let size = 2 * long + 8;
                    let mut buf = vec![0u8; size * 64];
                    loop {
                        match file.read(&mut buf) {
                            Ok(0) => break,
                            Ok(n) => {
                                for event in buf[..n].chunks_exact(size) {
                                    let kind = u16::from_ne_bytes([event[2 * long], event[2 * long + 1]]);
                                    let code = u16::from_ne_bytes([event[2 * long + 2], event[2 * long + 3]]);
                                    let value = i32::from_ne_bytes([
                                        event[2 * long + 4],
                                        event[2 * long + 5],
                                        event[2 * long + 6],
                                        event[2 * long + 7],
                                    ]);
                                    // EV_KEY, pressed (not released, not repeated).
                                    if kind == 1 && value == 1 {
                                        if let Some(key) = Media::from_evdev(code) {
                                            if tx.send(key).is_err() {
                                                return;
                                            }
                                        }
                                    }
                                }
                            }
                            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                            Err(_) => break,
                        }
                    }
                    if let Ok(mut set) = open.lock() {
                        set.remove(&path);
                    }
                });
                if reader.is_err() {
                    eprintln!("face: could not start a key reader; Mission Control and Launchpad are off");
                }
            }
        }
        // Keyboards can come and go (USB): look again now and then.
        std::thread::sleep(Duration::from_secs(3));
    });
    if started.is_err() {
        eprintln!("face: could not start the key watcher; Mission Control and Launchpad are off");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_printed_key_on_the_top_row_has_its_meaning() {
        let row = [
            (Scancode::BrightnessDown, "key.brightness", Some("down")),
            (Scancode::BrightnessUp, "key.brightness", Some("up")),
            (Scancode::KbdIllumDown, "key.kbd_light", Some("down")),
            (Scancode::KbdIllumUp, "key.kbd_light", Some("up")),
            (Scancode::KbdIllumToggle, "key.kbd_light", Some("toggle")),
            (Scancode::Mute, "key.mute_speaker", None),
            (Scancode::VolumeDown, "key.volume", Some("down")),
            (Scancode::VolumeUp, "key.volume", Some("up")),
        ];
        for (code, topic, dir) in row {
            let key = Media::from_scancode(code).unwrap_or_else(|| panic!("{code:?}"));
            match action(key, false) {
                Action::Send(ev) => {
                    assert_eq!(ev.topic, topic, "{code:?}");
                    assert_eq!(ev.str("dir"), dir, "{code:?}");
                }
                other => panic!("{code:?}: {other:?}"),
            }
        }
        assert_eq!(Media::from_evdev(KEY_SCALE), Some(Media::MissionControl));
        assert_eq!(Media::from_evdev(KEY_DASHBOARD), Some(Media::Launchpad));
        assert_eq!(action(Media::MissionControl, true), Action::Menu, "Mission Control opens the menu, as F2 does");
        assert!(matches!(action(Media::Launchpad, true), Action::Send(ev) if ev.topic == "key.apps"));
        for key in [Media::Previous, Media::Next, Media::Eject] {
            assert!(matches!(action(key, true), Action::Nothing(_)), "{key:?}");
        }
        // F-keys (fn held) and letters are not media keys: Pip's F1, F2,
        // F9 and F10 keep their own meaning.
        for code in [Scancode::F1, Scancode::F2, Scancode::F3, Scancode::F9, Scancode::F10, Scancode::M, Scancode::Space] {
            assert_eq!(Media::from_scancode(code), None, "{code:?}");
        }
        assert_eq!(Media::from_evdev(59), None, "KEY_F1");
    }

    #[test]
    fn play_pause_listens_while_resting_and_stops_a_reply_otherwise() {
        let topic = |resting| match action(Media::PlayPause, resting) {
            Action::Send(ev) => ev.topic,
            other => panic!("{other:?}"),
        };
        assert_eq!(topic(true), "key.attend");
        assert_eq!(topic(false), "key.interrupt");
    }

    #[test]
    fn a_held_level_key_steps_at_a_steady_pace_and_others_do_not_repeat() {
        let start = Instant::now();
        let at = |ms: u64| start + Duration::from_millis(ms);
        let mut r = Repeats::default();
        assert!(r.admit(Media::VolumeUp, false, at(0)));
        assert!(!r.admit(Media::VolumeUp, true, at(30)), "too soon");
        assert!(r.admit(Media::VolumeUp, true, at(120)));
        assert!(!r.admit(Media::VolumeUp, true, at(200)));
        assert!(r.admit(Media::VolumeUp, true, at(240)));
        assert!(r.admit(Media::VolumeDown, false, at(250)), "a fresh press always counts");
        assert!(r.admit(Media::BrightnessUp, true, at(260)), "another key's repeat is not held back by this one");
        assert!(r.admit(Media::Mute, false, at(270)));
        assert!(!r.admit(Media::Mute, true, at(900)), "mute toggles once per press");
        assert!(!r.admit(Media::PlayPause, true, at(1000)));
        assert!(!r.admit(Media::KbdLightToggle, true, at(1100)));
    }

    #[test]
    fn the_level_bar_shows_what_was_set_for_a_moment() {
        let start = Instant::now();
        let mut bar = LevelBar::default();
        assert!(!bar.visible(start) && bar.line(start).is_none());
        bar.event(&Event::new("body.brightness").with("ok", true).with("level", 50), start);
        assert!(bar.take_changed() && !bar.take_changed());
        let line = bar.line(start).unwrap();
        assert!(line.starts_with("brightness"), "{line}");
        assert_eq!(line.matches('█').count(), 8, "{line}");
        assert_eq!(line.matches('░').count(), 8, "{line}");
        assert!(line.ends_with("50%"), "{line}");
        assert!(bar.visible(start + Duration::from_millis(1400)));
        assert!(!bar.visible(start + LEVEL_SHOWN), "gone after a second and a half");
        assert!(bar.line(start + LEVEL_SHOWN).is_none());

        bar.event(&Event::new("voice.volume_state").with("level", 0).with("mixer", "hardware"), start);
        let line = bar.line(start).unwrap();
        assert!(line.starts_with("volume") && line.ends_with("muted") && !line.contains('█'), "{line}");
        bar.event(&Event::new("voice.volume_state").with("level", 3), start);
        assert_eq!(bar.line(start).unwrap().matches('█').count(), 1, "a little is never shown as none");
        bar.event(&Event::new("body.kbd_light").with("ok", true).with("level", 100).with("on", true), start);
        let line = bar.line(start).unwrap();
        assert!(line.starts_with("keyboard light") && line.matches('█').count() == SEGMENTS, "{line}");
        bar.event(&Event::new("body.kbd_light").with("ok", true).with("level", 0), start);
        assert!(bar.line(start).unwrap().ends_with("off"));
        // Every line is the same width, so the bar does not jump about.
        let widths: Vec<usize> = [("body.brightness", 5), ("voice.volume_state", 100), ("body.kbd_light", 0)]
            .iter()
            .map(|(topic, level)| {
                let mut b = LevelBar::default();
                b.event(&Event::new(*topic).with("ok", true).with("level", *level), start);
                b.line(start).unwrap().chars().count()
            })
            .collect();
        assert!(widths.windows(2).all(|w| w[0] == w[1]), "{widths:?}");
    }

    #[test]
    fn a_muted_speaker_stays_marked_after_the_bar_goes() {
        let start = Instant::now();
        let later = start + LEVEL_SHOWN + Duration::from_secs(600);
        let mut bar = LevelBar::default();
        bar.event(&Event::new("voice.volume_state").with("level", 0).with("mixer", "hardware"), start);
        assert!(bar.line(start).unwrap().starts_with("volume"), "the bar first");
        assert!(!bar.only_muted_note(start));
        assert_eq!(bar.line(later).as_deref(), Some(SPEAKER_MUTED), "then the lasting note");
        assert!(bar.only_muted_note(later));
        // Another gauge's bar shows over it, and the note comes back after.
        bar.event(&Event::new("body.brightness").with("ok", true).with("level", 40), later);
        assert!(bar.line(later).unwrap().starts_with("brightness"));
        assert_eq!(bar.line(later + LEVEL_SHOWN).as_deref(), Some(SPEAKER_MUTED));
        // Any sound at all takes the note away.
        bar.event(&Event::new("voice.volume_state").with("level", 6), later);
        assert!(bar.line(later + LEVEL_SHOWN).is_none());
        assert!(!bar.only_muted_note(later + LEVEL_SHOWN));
        // A strange report changes nothing.
        bar.event(&Event::new("voice.volume_state").with("level", 0), later);
        bar.event(&Event::new("voice.volume_state").with("level", "loud"), later);
        assert_eq!(bar.line(later + LEVEL_SHOWN).as_deref(), Some(SPEAKER_MUTED));
    }

    #[test]
    fn a_failed_or_strange_report_shows_nothing() {
        let now = Instant::now();
        let mut bar = LevelBar::default();
        for ev in [
            Event::new("body.brightness").with("ok", false).with("reason", "no_control"),
            Event::new("body.brightness").with("ok", false).with("reason", "write_failed").with("level", 40),
            Event::new("body.kbd_light").with("ok", false).with("reason", "no_control"),
            Event::new("voice.volume_state").with("level", 140),
            Event::new("voice.volume_state").with("level", "loud"),
            Event::new("body.state").with("level", 50),
        ] {
            bar.event(&ev, now);
            assert!(!bar.visible(now), "{ev:?}");
        }
        assert!(!bar.take_changed());
    }

    #[test]
    fn the_keyboard_capabilities_are_read_as_the_kernel_writes_them() {
        // The MacBookPro9,2's internal keyboard (hid-apple sets every key it
        // translates to, KEY_SCALE and KEY_DASHBOARD among them), as five
        // 64-bit words; and the lid switch, which has no keys at all.
        let mut words = [0u64; 5];
        for code in [1u16, 59, 113, 120, 204, 224, 229, 230] {
            words[usize::from(code) / 64] |= 1 << (code % 64);
        }
        let caps = words.iter().rev().map(|w| format!("{w:x}")).collect::<Vec<_>>().join(" ");
        assert!(has_key(&caps, KEY_SCALE, 64) && has_key(&caps, KEY_DASHBOARD, 64) && has_key(&caps, 1, 64));
        assert!(!has_key(&caps, 121, 64) && !has_key(&caps, 400, 64));
        assert!(!has_key("0", KEY_SCALE, 64));
        assert!(!has_key("", KEY_DASHBOARD, 64));
        assert!(!has_key("zz 0 0 0", KEY_DASHBOARD, 64));
        // A 32-bit kernel writes 32-bit words.
        let short = format!("{:x} 0 0 {:x} 0 0 0", 1u32 << (204 - 192), 1u32 << (120 - 96));
        assert!(has_key(&short, KEY_SCALE, 32) && has_key(&short, KEY_DASHBOARD, 32));
    }
}

//! The Quick Menu's permanent dock: a slim pill at the top centre of the full
//! face, with the menu, the way Pip is being talked to (spoken, written with
//! spoken replies, written only) and, in the written modes, the box the owner
//! types into.
//!
//! The dock is not a surface. It never docks the eyes into their strip, never
//! raises the frame rate and never takes the idle frame skip away: it is one
//! small picture, drawn only when its content changes (`revision`) and faded
//! by alpha (`alpha_step`), both of which stop changing within a second of
//! the owner's last input. The mode itself belongs to the mind (`chat.mode`);
//! the face only shows it, and asks for a change with `ui.action`.

use crate::{
    chrome::{self, Color, DIM, EDGE, FAINT, GLOW, INK, WARNING},
    panel::Rect,
    text::{Font, Picture},
};
use bus::Event;
use std::time::{Duration, Instant};

/// The longest line the owner can type (the bridge takes the same).
pub const MAX_LINE: usize = 400;
/// Longest notice kept, in characters.
const NOTICE_MAX: usize = 120;
/// A notice from the bridge stays this long.
const NOTICE_HOLD: Duration = Duration::from_secs(6);
/// After this long without input the dock goes back to a pill and fades.
pub const IDLE: Duration = Duration::from_secs(20);
/// How long the fade takes, and what is left of the dock after it.
const FADE: Duration = Duration::from_millis(600);
const FADED: f32 = 0.35;
/// Alpha is reported in this many steps, so a fade is a few redraws, not many.
const STEPS: u8 = 16;

const PILL_W: i32 = 420;
const PILL_H: i32 = 48;
const BAR_W: i32 = 900;
const BAR_H: i32 = 56;
/// The dock's top edge, in screen pixels at the 800-row size.
const TOP: i32 = 14;
const CHIP_FILL: Color = [6, 21, 27];

/// How Pip is talked to. The mind publishes it as `chat.mode`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Mode {
    /// Talk and hear (as always).
    #[default]
    Spoken,
    /// Type; Pip answers aloud and the words show as they are spoken.
    TypedVoice,
    /// Type; Pip stays silent and answers in text.
    TypedText,
}

impl Mode {
    pub fn parse(name: &str) -> Option<Mode> {
        match name {
            "spoken" => Some(Mode::Spoken),
            "typed_voice" => Some(Mode::TypedVoice),
            "typed_text" => Some(Mode::TypedText),
            _ => None,
        }
    }

    /// The mode a `chat.mode` message names: its `mode`, or else its
    /// `input` and `reply` fields.
    fn from_event(ev: &Event) -> Option<Mode> {
        if let Some(mode) = ev.str("mode").and_then(Mode::parse) {
            return Some(mode);
        }
        match (ev.str("input")?, ev.str("reply")?) {
            ("voice", _) => Some(Mode::Spoken),
            ("typed", "voice") => Some(Mode::TypedVoice),
            ("typed", "text") => Some(Mode::TypedText),
            _ => None,
        }
    }

    /// The words on the chip.
    pub fn label(self) -> &'static str {
        match self {
            Mode::Spoken => "Spoken",
            Mode::TypedVoice => "Written + voice",
            Mode::TypedText => "Written only",
        }
    }

    /// Whether the owner types to Pip in this mode.
    pub fn typed(self) -> bool {
        self != Mode::Spoken
    }

    fn colour(self) -> Color {
        if self == Mode::TypedText { WARNING } else { GLOW }
    }
}

/// The owner's Layout choice for the dock.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Setting {
    /// Always at full strength.
    Always,
    /// Always there, and fading to a pill when idle.
    #[default]
    Fades,
    Off,
}

impl Setting {
    pub fn parse(name: &str) -> Setting {
        match name {
            "always" => Setting::Always,
            "off" => Setting::Off,
            _ => Setting::Fades,
        }
    }
}

/// What the face knows that hides the dock.
#[derive(Clone, Copy, Debug, Default)]
pub struct Gate {
    /// A guest is here: nothing of the owner's may show.
    pub guest: bool,
    /// The panel is dark (the lid is closed, or the screen is off).
    pub blank: bool,
    /// The eyes are asleep.
    pub asleep: bool,
    /// The brainstem has gone quiet: nothing the dock sends would arrive.
    pub quiet: bool,
    /// The menu, Options or an app panel has the screen (or is leaving it).
    pub surface_open: bool,
}

/// A part of the dock a click can land on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Hit {
    Menu,
    Chip,
    Field,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Tone {
    Warning,
    Muted,
}

struct Notice {
    text: String,
    tone: Tone,
    at: Instant,
}

/// The dock's geometry in its own pixels: the picture size and the parts.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Layout {
    pub w: i32,
    pub h: i32,
    pub menu: Rect,
    pub chip: Rect,
    /// The typing box (the width the app names take when collapsed).
    pub field: Rect,
}

impl Layout {
    /// `max_w` is the screen's width: the dock never spills over its edges.
    pub fn new(expanded: bool, mode: Mode, max_w: i32) -> Layout {
        let (w, h) = if expanded { (BAR_W, BAR_H) } else { (PILL_W, PILL_H) };
        let w = w.min((max_w - 32).max(200));
        let chip_w = mode.label().chars().count() as i32 * 8 + 28;
        let chip = Rect { x: w - 12 - chip_w, y: (h - 32) / 2, w: chip_w, h: 32 };
        let menu = Rect { x: 12, y: (h - 32) / 2, w: 60, h: 32 };
        let field = Rect { x: menu.x + menu.w + 14, y: (h - 32) / 2, w: (chip.x - 14 - (menu.x + menu.w + 14)).max(0), h: 32 };
        Layout { w, h, menu, chip, field }
    }

    fn contains(rect: Rect, x: i32, y: i32) -> bool {
        x >= rect.x && x < rect.x + rect.w && y >= rect.y && y < rect.y + rect.h
    }

    /// What a point in the dock's own pixels lands on.
    pub fn hit(&self, x: i32, y: i32) -> Option<Hit> {
        if x < 0 || y < 0 || x >= self.w || y >= self.h {
            return None;
        }
        if Self::contains(self.chip, x, y) {
            Some(Hit::Chip)
        } else if Self::contains(self.menu, x, y) {
            Some(Hit::Menu)
        } else {
            Some(Hit::Field)
        }
    }
}

/// Where a dock picture `pic_w` by `pic_h` sits on a `w` by `h` screen:
/// centred at the top, at whole multiples of its own size on a taller screen.
/// The rectangle is in screen pixels, top row first (a click's coordinates).
pub fn place(pic_w: i32, pic_h: i32, w: i32, h: i32) -> Rect {
    let mut zoom = (h / 800).max(1);
    while zoom > 1 && pic_w * zoom > w {
        zoom -= 1;
    }
    Rect { x: (w - pic_w * zoom) / 2, y: TOP * zoom, w: pic_w * zoom, h: pic_h * zoom }
}

pub fn screen_rect(layout: &Layout, w: i32, h: i32) -> Rect {
    place(layout.w, layout.h, w, h)
}

/// The same rectangle for the texture quad, whose rows count from the bottom.
pub fn draw_rect(pic_w: i32, pic_h: i32, w: i32, h: i32) -> Rect {
    let top = place(pic_w, pic_h, w, h);
    Rect { y: h - top.y - top.h, ..top }
}

/// What a click at screen pixel (`x`, `y`) on a `w` by `h` screen lands on.
pub fn click(layout: &Layout, w: i32, h: i32, x: i32, y: i32) -> Option<Hit> {
    let rect = screen_rect(layout, w, h);
    let zoom = (rect.w / layout.w).max(1);
    layout.hit((x - rect.x).div_euclid(zoom), (y - rect.y).div_euclid(zoom))
}

#[derive(Default)]
pub struct Dock {
    mode: Mode,
    composer: String,
    notice: Option<Notice>,
    last_input: Option<Instant>,
    /// Counts every change to what the dock's picture shows.
    revision: u64,
    visible: bool,
    /// The dock would show were no surface open (the mode key needs this).
    available: bool,
    expanded: bool,
    alpha: f32,
}

impl Dock {
    pub fn mode(&self) -> Mode {
        self.mode
    }

    pub fn visible(&self) -> bool {
        self.visible
    }

    /// Whether the mode key may be taken now.
    pub fn available(&self) -> bool {
        self.available
    }

    /// Whether typed text goes to the dock's box now.
    pub fn capturing(&self) -> bool {
        self.visible && self.mode.typed()
    }

    #[cfg(test)]
    pub fn expanded(&self) -> bool {
        self.expanded
    }

    #[cfg(test)]
    pub fn text(&self) -> &str {
        &self.composer
    }

    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn alpha(&self) -> f32 {
        self.alpha
    }

    /// The fade, in a few steps: it changes only while the dock is fading.
    pub fn alpha_step(&self) -> u8 {
        (self.alpha * f32::from(STEPS)).round() as u8
    }

    /// Everything that decides how the dock looks, for the idle frame skip:
    /// the same value twice means the same picture.
    pub fn look_key(&self) -> Option<(u64, u8)> {
        self.visible.then(|| (self.revision, self.alpha_step()))
    }

    pub fn layout(&self, screen_w: i32) -> Layout {
        Layout::new(self.expanded, self.mode, screen_w)
    }

    fn touch(&mut self, now: Instant) {
        self.last_input = Some(now);
    }

    fn changed(&mut self) {
        self.revision = self.revision.wrapping_add(1);
    }

    /// The mind says how Pip is talked to now (`chat.mode`), or the bridge
    /// has something to tell the owner (`chat.notice`).
    pub fn event(&mut self, ev: &Event, now: Instant) {
        match ev.topic.as_str() {
            "chat.mode" => {
                if let Some(mode) = Mode::from_event(ev) {
                    self.set_mode(mode, now);
                }
            }
            "chat.notice" => {
                let text = printable(ev.str("text").unwrap_or(""), NOTICE_MAX);
                if text.is_empty() {
                    return;
                }
                let tone = if ev.str("tone") == Some("muted") { Tone::Muted } else { Tone::Warning };
                self.notice = Some(Notice { text, tone, at: now });
                self.touch(now);
                self.changed();
            }
            _ => {}
        }
    }

    fn set_mode(&mut self, mode: Mode, now: Instant) {
        if mode == self.mode {
            return;
        }
        self.mode = mode;
        self.touch(now);
        if !mode.typed() {
            self.composer.clear();
            self.notice = None;
        }
        self.changed();
    }

    /// Decide, once a frame, whether the dock shows and how. A guest, a dark
    /// panel or a spoken mode leaves no typed text behind.
    pub fn update(&mut self, gate: Gate, setting: Setting, now: Instant) {
        let last = *self.last_input.get_or_insert(now);
        if (gate.guest || gate.blank || !self.mode.typed()) && !self.composer.is_empty() {
            self.composer.clear();
            self.changed();
        }
        if self.notice.as_ref().is_some_and(|notice| now.saturating_duration_since(notice.at) >= NOTICE_HOLD) {
            self.notice = None;
            self.changed();
        }
        if gate.guest && self.notice.is_some() {
            self.notice = None;
            self.changed();
        }
        self.available = setting != Setting::Off && !gate.guest && !gate.blank && !gate.asleep && !gate.quiet;
        self.visible = self.available && !gate.surface_open;
        let idle = now.saturating_duration_since(last);
        let recent = idle < IDLE;
        let expanded = self.visible && self.mode.typed()
            && (setting == Setting::Always || !self.composer.is_empty() || self.notice.is_some() || recent);
        if expanded != self.expanded {
            self.expanded = expanded;
            self.changed();
        }
        self.alpha = if setting == Setting::Always || recent || !self.composer.is_empty() || self.notice.is_some() {
            1.0
        } else {
            let faded = (idle - IDLE).as_secs_f32() / FADE.as_secs_f32();
            1.0 - (1.0 - FADED) * faded.clamp(0.0, 1.0)
        };
    }

    /// Typed text (SDL's text input): printable characters only.
    pub fn input(&mut self, text: &str, now: Instant) {
        if !self.capturing() {
            return;
        }
        let mut changed = false;
        for ch in text.chars().filter(|ch| !ch.is_control()) {
            if self.composer.chars().count() >= MAX_LINE {
                break;
            }
            self.composer.push(ch);
            changed = true;
        }
        if changed {
            self.touch(now);
            self.notice = None;
            self.changed();
        }
    }

    pub fn backspace(&mut self, now: Instant) {
        if self.composer.pop().is_some() {
            self.touch(now);
            self.changed();
        }
    }

    /// Enter: the finished line, if there is one. The box is emptied.
    pub fn submit(&mut self, now: Instant) -> Option<String> {
        let line = self.composer.split_whitespace().collect::<Vec<_>>().join(" ");
        if self.composer.is_empty() {
            return None;
        }
        self.composer.clear();
        self.touch(now);
        self.changed();
        (!line.is_empty()).then_some(line)
    }

    /// Esc: empties the box and says so. With nothing typed it is not the
    /// dock's key (Esc then stops Pip speaking, as everywhere).
    pub fn escape(&mut self, now: Instant) -> bool {
        if self.composer.is_empty() {
            return false;
        }
        self.composer.clear();
        self.touch(now);
        self.changed();
        true
    }

    /// A click or key on the dock counts as use, so it stays in view.
    pub fn used(&mut self, now: Instant) {
        self.touch(now);
    }

    pub fn picture(&self, font: &Font, screen_w: i32) -> Picture {
        let layout = self.layout(screen_w);
        let mut picture = chrome::picture(layout.w as usize, layout.h as usize);
        let outline = if self.mode.typed() { self.mode.colour() } else { EDGE };
        picture.glass(Rect { x: 0, y: 0, w: layout.w, h: layout.h }, layout.h / 2);
        if self.mode.typed() {
            // A thin ring in the mode's colour, so the written modes read at a glance.
            picture.fill(Rect { x: layout.h / 2, y: 0, w: layout.w - layout.h, h: 1 }, 0, outline);
            picture.fill(Rect { x: layout.h / 2, y: layout.h - 1, w: layout.w - layout.h, h: 1 }, 0, outline);
        }
        let text_y = |rect: Rect| rect.y + (rect.h - font.h as i32) / 2;
        font.draw(&mut picture, layout.menu.x + 8, text_y(layout.menu), "Menu", INK, 1);
        let columns = (layout.field.w / font.w as i32).max(0) as usize;
        let (line, colour) = self.field_line(columns);
        font.draw(&mut picture, layout.field.x, text_y(layout.field), &line, colour, 1);
        if self.expanded && self.notice.is_none() {
            // The typing position: a still bar (nothing blinks, so nothing redraws).
            let x = layout.field.x + line.chars().count() as i32 * font.w as i32 + 1;
            if self.composer.is_empty() {
                picture.fill(Rect { x: layout.field.x - 4, y: layout.field.y + 6, w: 2, h: 20 }, 0, GLOW);
            } else if x < layout.field.x + layout.field.w {
                picture.fill(Rect { x, y: layout.field.y + 6, w: 2, h: 20 }, 0, GLOW);
            }
        }
        let colour = self.mode.colour();
        picture.fill(layout.chip, 16, colour);
        picture.fill(
            Rect { x: layout.chip.x + 2, y: layout.chip.y + 2, w: layout.chip.w - 4, h: layout.chip.h - 4 },
            14,
            CHIP_FILL,
        );
        let label = self.mode.label();
        let x = layout.chip.x + (layout.chip.w - label.chars().count() as i32 * font.w as i32) / 2;
        font.draw(&mut picture, x, text_y(layout.chip), label, colour, 1);
        picture
    }

    /// What the dock's middle says: a notice, what is being typed (its tail),
    /// a hint to type, or the apps the menu leads to.
    fn field_line(&self, columns: usize) -> (String, Color) {
        let fit = |text: &str| {
            let count = text.chars().count();
            if count <= columns {
                text.to_string()
            } else {
                let tail: String = text.chars().skip(count - columns.saturating_sub(1)).collect();
                format!("…{tail}")
            }
        };
        if let Some(notice) = &self.notice {
            return (fit(&notice.text), if notice.tone == Tone::Warning { WARNING } else { DIM });
        }
        if !self.composer.is_empty() {
            return (fit(&self.composer), INK);
        }
        if self.mode.typed() {
            let hint = if self.expanded { "Type to Pip, then Enter" } else { "Start typing to Pip" };
            return (fit(hint), FAINT);
        }
        (fit("Lists  Memory  Journal  Mind"), DIM)
    }
}

/// Text as it may be drawn: no control characters, spaces collapsed, cut to `limit`.
fn printable(text: &str, limit: usize) -> String {
    let clean: String = text.chars().map(|ch| if ch.is_control() { ' ' } else { ch }).collect();
    clean.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(limit).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gate() -> Gate {
        Gate::default()
    }

    fn mode_event(mode: &str) -> Event {
        Event::new("chat.mode").with("mode", mode)
    }

    fn typed_dock(now: Instant) -> Dock {
        let mut dock = Dock::default();
        dock.event(&mode_event("typed_voice"), now);
        dock.update(gate(), Setting::Fades, now);
        dock
    }

    #[test]
    fn the_modes_parse_from_the_bus_and_label_the_chip() {
        let now = Instant::now();
        let mut dock = Dock::default();
        assert_eq!(dock.mode(), Mode::Spoken);
        assert_eq!(dock.mode().label(), "Spoken");
        for (name, mode, label) in [
            ("typed_voice", Mode::TypedVoice, "Written + voice"),
            ("typed_text", Mode::TypedText, "Written only"),
            ("spoken", Mode::Spoken, "Spoken"),
        ] {
            dock.event(&mode_event(name), now);
            assert_eq!(dock.mode(), mode);
            assert_eq!(mode.label(), label);
        }
        // `input` and `reply` stand in for a message with no `mode`.
        dock.event(&Event::new("chat.mode").with("input", "typed").with("reply", "text"), now);
        assert_eq!(dock.mode(), Mode::TypedText);
        dock.event(&Event::new("chat.mode").with("input", "voice").with("reply", "voice"), now);
        assert_eq!(dock.mode(), Mode::Spoken);
        // An unknown mode changes nothing.
        dock.event(&mode_event("typed_text"), now);
        dock.event(&mode_event("telepathy"), now);
        assert_eq!(dock.mode(), Mode::TypedText);
        assert!(Mode::TypedVoice.typed() && Mode::TypedText.typed() && !Mode::Spoken.typed());
    }

    #[test]
    fn a_mode_change_redraws_and_going_back_to_spoken_empties_the_box() {
        let now = Instant::now();
        let mut dock = typed_dock(now);
        let before = dock.revision();
        dock.input("hello", now);
        assert_eq!(dock.text(), "hello");
        dock.event(&mode_event("typed_text"), now);
        assert!(dock.revision() > before);
        assert_eq!(dock.text(), "hello", "typed to typed keeps the draft");
        dock.event(&mode_event("spoken"), now);
        dock.update(gate(), Setting::Fades, now);
        assert_eq!(dock.text(), "");
        assert!(!dock.capturing() && !dock.expanded());
        // The same mode again is not a change.
        let settled = dock.revision();
        dock.event(&mode_event("spoken"), now);
        assert_eq!(dock.revision(), settled);
    }

    #[test]
    fn the_box_takes_printable_text_edits_it_and_stops_at_the_cap() {
        let now = Instant::now();
        let mut dock = typed_dock(now);
        dock.input("set a ti\u{7}mer\n", now);
        assert_eq!(dock.text(), "set a timer", "control characters never reach the box");
        dock.backspace(now);
        dock.backspace(now);
        assert_eq!(dock.text(), "set a tim");
        dock.input(&"x".repeat(1000), now);
        assert_eq!(dock.text().chars().count(), MAX_LINE);
        dock.input("more", now);
        assert_eq!(dock.text().chars().count(), MAX_LINE, "the cap holds");
        // Esc empties a typed line, and is not the dock's key once it is empty.
        assert!(dock.escape(now));
        assert_eq!(dock.text(), "");
        assert!(!dock.escape(now));
        dock.backspace(now);
        assert_eq!(dock.text(), "");
    }

    #[test]
    fn enter_gives_the_trimmed_line_once_and_nothing_for_an_empty_box() {
        let now = Instant::now();
        let mut dock = typed_dock(now);
        assert_eq!(dock.submit(now), None);
        dock.input("  what   time is it  ", now);
        assert_eq!(dock.submit(now).as_deref(), Some("what time is it"));
        assert_eq!(dock.text(), "");
        assert_eq!(dock.submit(now), None);
        dock.input("   ", now);
        assert_eq!(dock.submit(now), None, "blanks are not a message");
        assert_eq!(dock.text(), "", "and they are cleared");
    }

    #[test]
    fn a_spoken_dock_takes_no_typing() {
        let now = Instant::now();
        let mut dock = Dock::default();
        dock.update(gate(), Setting::Fades, now);
        assert!(dock.visible() && !dock.capturing());
        dock.input("hello", now);
        assert_eq!(dock.text(), "");
    }

    #[test]
    fn the_dock_hides_for_a_guest_a_dark_panel_sleep_a_quiet_brainstem_a_surface_and_off() {
        let now = Instant::now();
        let mut dock = typed_dock(now);
        assert!(dock.visible() && dock.capturing());
        let hidden = |gate: Gate, setting: Setting| {
            let mut dock = typed_dock(now);
            dock.update(gate, setting, now);
            (dock.visible(), dock.capturing(), dock.available())
        };
        assert_eq!(hidden(Gate { guest: true, ..gate() }, Setting::Fades), (false, false, false));
        assert_eq!(hidden(Gate { blank: true, ..gate() }, Setting::Fades), (false, false, false));
        assert_eq!(hidden(Gate { asleep: true, ..gate() }, Setting::Fades), (false, false, false));
        assert_eq!(hidden(Gate { quiet: true, ..gate() }, Setting::Fades), (false, false, false));
        assert_eq!(hidden(gate(), Setting::Off), (false, false, false));
        // A surface hides it, but the mode key still works behind the surface.
        assert_eq!(hidden(Gate { surface_open: true, ..gate() }, Setting::Fades), (false, false, true));
        dock.update(gate(), Setting::Always, now);
        assert!(dock.visible());
    }

    #[test]
    fn a_guest_or_a_closed_lid_leaves_no_typed_text_or_notice_behind() {
        let now = Instant::now();
        for hide in [Gate { guest: true, ..gate() }, Gate { blank: true, ..gate() }] {
            let mut dock = typed_dock(now);
            dock.input("my secret list", now);
            dock.event(&Event::new("chat.notice").with("text", "Pip is muted."), now);
            dock.update(hide, Setting::Fades, now);
            assert_eq!(dock.text(), "");
            dock.update(gate(), Setting::Fades, now);
            assert_eq!(dock.text(), "", "and it does not come back");
            if hide.guest {
                assert!(dock.notice.is_none(), "a guest never sees the owner's notices");
            }
        }
    }

    #[test]
    fn the_dock_expands_to_a_box_in_a_written_mode_and_fades_to_a_pill_when_idle() {
        let start = Instant::now();
        let mut dock = typed_dock(start);
        assert!(dock.expanded());
        assert_eq!(dock.alpha_step(), STEPS);
        // Still in view just inside the idle time.
        dock.update(gate(), Setting::Fades, start + IDLE - Duration::from_secs(1));
        assert!(dock.expanded() && dock.alpha_step() == STEPS);
        // Idle: the box folds into the pill and fades, in a few steps.
        let mut last = STEPS;
        let mut steps = 0;
        for ms in (0..=1000).step_by(33) {
            dock.update(gate(), Setting::Fades, start + IDLE + Duration::from_millis(ms));
            assert!(!dock.expanded());
            assert!(dock.alpha_step() <= last, "a fade only goes one way");
            if dock.alpha_step() != last {
                steps += 1;
            }
            last = dock.alpha_step();
        }
        assert_eq!(last, (FADED * f32::from(STEPS)).round() as u8);
        assert!(steps <= usize::from(STEPS));
        // Typing brings it straight back, at full strength.
        dock.input("h", start + IDLE + Duration::from_secs(2));
        dock.update(gate(), Setting::Fades, start + IDLE + Duration::from_secs(2));
        assert!(dock.expanded() && dock.alpha_step() == STEPS);
    }

    #[test]
    fn always_never_fades_and_keeps_the_box_open() {
        let start = Instant::now();
        let mut dock = typed_dock(start);
        dock.update(gate(), Setting::Always, start + Duration::from_secs(600));
        assert!(dock.expanded());
        assert_eq!(dock.alpha_step(), STEPS);
    }

    #[test]
    fn a_spoken_dock_is_a_pill_that_fades() {
        let start = Instant::now();
        let mut dock = Dock::default();
        dock.update(gate(), Setting::Fades, start);
        assert!(dock.visible() && !dock.expanded());
        dock.update(gate(), Setting::Fades, start + IDLE + FADE);
        assert_eq!(dock.alpha_step(), (FADED * f32::from(STEPS)).round() as u8);
    }

    #[test]
    fn an_idle_dock_asks_for_no_redraw_at_all() {
        // The host measurement for "the dock does not raise the idle frame
        // rate": a visible dock at rest at 30 Hz for a minute changes what
        // it asks to draw only during its one fade, then never again. The
        // frame loop draws a settled face only when this key changes.
        let start = Instant::now();
        for mode in ["spoken", "typed_voice", "typed_text"] {
            let mut dock = Dock::default();
            dock.event(&mode_event(mode), start);
            let mut keys = Vec::new();
            for frame in 0..(30 * 60) {
                dock.update(gate(), Setting::Fades, start + Duration::from_millis(frame * 33));
                let key = dock.look_key();
                assert!(key.is_some());
                if keys.last() != Some(&key) {
                    keys.push(key);
                }
            }
            // Full strength, (typed modes: the box folding away,) the fade steps, then still.
            assert!(keys.len() <= 2 + usize::from(STEPS), "{mode}: {} changes in a minute", keys.len());
            let settled = keys.last().copied().flatten();
            let mut again = dock.look_key();
            for frame in (30 * 60)..(30 * 120) {
                dock.update(gate(), Setting::Fades, start + Duration::from_millis(frame * 33));
                again = dock.look_key();
                assert_eq!(again, settled, "{mode}: it must stay put once faded");
            }
            assert_eq!(again, settled);
        }
        // Hidden, it asks for nothing.
        let mut dock = Dock::default();
        dock.update(Gate { blank: true, ..gate() }, Setting::Fades, start);
        assert_eq!(dock.look_key(), None);
    }

    #[test]
    fn the_dock_never_counts_as_a_surface_and_a_surface_hides_it() {
        // surface_open in the frame loop is built from the app panel, the
        // menu and Options alone; the dock is on the other side of the gate.
        let now = Instant::now();
        let mut dock = typed_dock(now);
        assert!(dock.visible());
        dock.update(Gate { surface_open: true, ..gate() }, Setting::Fades, now);
        assert!(!dock.visible(), "a menu has the screen to itself");
        assert_eq!(dock.look_key(), None);
        dock.update(gate(), Setting::Fades, now);
        assert!(dock.visible());
    }

    #[test]
    fn notices_show_in_the_dock_for_a_while_then_go() {
        let start = Instant::now();
        let mut dock = typed_dock(start);
        dock.event(&Event::new("chat.notice").with("text", "Pip is\nmuted.").with("tone", "warning"), start);
        assert_eq!(dock.field_line(60).0, "Pip is muted.");
        assert_eq!(dock.field_line(60).1, WARNING);
        dock.event(&Event::new("chat.notice").with("text", ""), start);
        assert_eq!(dock.field_line(60).0, "Pip is muted.", "an empty notice changes nothing");
        let long = "n".repeat(500);
        dock.event(&Event::new("chat.notice").with("text", long.as_str()).with("tone", "muted"), start);
        assert_eq!(dock.field_line(1000).0.chars().count(), NOTICE_MAX);
        assert_eq!(dock.field_line(10).0.chars().count(), 10, "and it fits the box");
        dock.update(gate(), Setting::Fades, start + NOTICE_HOLD);
        assert!(dock.field_line(60).0.starts_with("Start typing") || dock.field_line(60).0.starts_with("Type to"));
    }

    #[test]
    fn the_typed_text_shows_its_tail_when_it_is_too_long_for_the_box() {
        let now = Instant::now();
        let mut dock = typed_dock(now);
        dock.input("abcdefghij", now);
        assert_eq!(dock.field_line(20).0, "abcdefghij");
        assert_eq!(dock.field_line(5).0, "…ghij");
    }

    #[test]
    fn clicks_land_on_the_menu_the_chip_or_the_box() {
        for (expanded, mode) in [(false, Mode::Spoken), (true, Mode::TypedVoice), (true, Mode::TypedText)] {
            let layout = Layout::new(expanded, mode, 1280);
            assert_eq!(layout.hit(layout.menu.x + 2, layout.menu.y + 2), Some(Hit::Menu));
            assert_eq!(layout.hit(layout.chip.x + 2, layout.chip.y + 2), Some(Hit::Chip));
            assert_eq!(layout.hit(layout.field.x + 2, layout.field.y + 2), Some(Hit::Field));
            assert_eq!(layout.hit(-1, 5), None);
            assert_eq!(layout.hit(layout.w, 5), None);
            assert_eq!(layout.hit(5, layout.h), None);
            assert!(layout.chip.x >= layout.field.x + layout.field.w, "the chip never overlaps the box");
        }
    }

    #[test]
    fn the_dock_sits_at_the_top_centre_and_clear_of_the_eyes_strip() {
        for (w, h) in [(1280, 800), (1920, 1200), (640, 400)] {
            for (expanded, mode) in [(false, Mode::Spoken), (true, Mode::TypedText)] {
                let layout = Layout::new(expanded, mode, w);
                let top = screen_rect(&layout, w, h);
                assert!(top.x >= 0 && top.x + top.w <= w, "inside the screen at {w}x{h}");
                assert_eq!(top.x * 2 + top.w, w, "centred");
                let zoom = (h / 800).max(1);
                assert_eq!(top.y, TOP * zoom);
                // The eyes sit in the middle of the full face, well below the dock.
                assert!(top.y + top.h < h / 4, "clear of the eyes");
                let gl = draw_rect(layout.w, layout.h, w, h);
                assert_eq!(gl.y + gl.h, h - top.y, "the quad's rows count from the bottom");
            }
        }
        // 1280 wide: the dock is 420 collapsed and 900 as the box.
        assert_eq!(Layout::new(false, Mode::Spoken, 1280).w, 420);
        assert_eq!(Layout::new(true, Mode::TypedText, 1280).w, 900);
    }

    #[test]
    fn a_click_is_mapped_through_the_dock_s_place_on_the_screen() {
        for (w, h) in [(1280, 800), (2560, 1600)] {
            let layout = Layout::new(true, Mode::TypedVoice, w);
            let rect = screen_rect(&layout, w, h);
            let zoom = rect.w / layout.w;
            let at = |part: Rect| (rect.x + (part.x + 2) * zoom, rect.y + (part.y + 2) * zoom);
            let (x, y) = at(layout.chip);
            assert_eq!(click(&layout, w, h, x, y), Some(Hit::Chip));
            let (x, y) = at(layout.menu);
            assert_eq!(click(&layout, w, h, x, y), Some(Hit::Menu));
            let (x, y) = at(layout.field);
            assert_eq!(click(&layout, w, h, x, y), Some(Hit::Field));
            assert_eq!(click(&layout, w, h, rect.x - 1, rect.y + 5), None, "left of the dock");
            assert_eq!(click(&layout, w, h, rect.x + 5, rect.y - 1), None, "above the dock");
            assert_eq!(click(&layout, w, h, rect.x + 5, rect.y + rect.h), None, "below the dock");
            assert_eq!(click(&layout, w, h, w / 2, h / 2), None, "a click on the eyes is not the dock's");
        }
    }

    #[test]
    fn every_state_paints_a_picture_of_the_size_it_says() {
        let Some(font) = crate::text::font() else { return };
        let now = Instant::now();
        let mut dock = typed_dock(now);
        for mode in ["spoken", "typed_voice", "typed_text"] {
            dock.event(&mode_event(mode), now);
            dock.update(gate(), Setting::Always, now);
            let layout = dock.layout(1280);
            let picture = dock.picture(font, 1280);
            assert_eq!((picture.w as i32, picture.h as i32), (layout.w, layout.h));
            assert!(picture.rgba.iter().any(|byte| *byte != 0));
        }
    }
}

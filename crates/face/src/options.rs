//! The keyboard Options surface under the docked eyes.
//!
//! Controls render the latest applied preferences. Edits are requests to Mind,
//! which validates and saves them before they appear in this surface.

use crate::{
    chrome::{self, DIM, EDGE, GLOW, INK, WARNING},
    colour::Palette,
    look::Prefs,
    panel::Rect,
    render,
    text::{Font, Picture},
};
use sdl2::keyboard::{Keycode, Mod};
use serde_json::{json, Value};

const WIDTH: i32 = 832;
const HEIGHT: i32 = 496;
const FOCUS: chrome::Color = [16, 41, 54];
const CHIP: chrome::Color = [9, 20, 28];
const SELECTED: chrome::Color = [23, 57, 70];
const HELP_ROWS: usize = 11;
const SETTINGS_TOP: i32 = 106;
const FOOTER_TOP: i32 = 455;

#[derive(Debug, PartialEq)]
pub struct Change {
    pub field: &'static str,
    pub value: Value,
}

type Choice = (&'static str, &'static str);

#[derive(Clone, Copy)]
enum Control {
    Choices(&'static [Choice]),
    Palette,
    Toggle,
    Strength,
}

struct Setting {
    field: &'static str,
    title: &'static str,
    control: Control,
}

impl Setting {
    const fn new(field: &'static str, title: &'static str, control: Control) -> Self {
        Self {
            field,
            title,
            control,
        }
    }
}

const CORNERS: &[Choice] = &[
    ("top-left", "Top left"),
    ("top-right", "Top right"),
    ("bottom-left", "Bottom left"),
    ("bottom-right", "Bottom right"),
];
const LOOK: &[Setting] = &[
    Setting::new("glow", "Eye colour", Control::Palette),
    Setting::new(
        "shape",
        "Eye shape",
        Control::Choices(&[("soft", "Soft"), ("sharp", "Sharp"), ("round", "Round")]),
    ),
    Setting::new(
        "size",
        "Face size",
        Control::Choices(&[("small", "Small"), ("medium", "Medium"), ("full", "Full")]),
    ),
    Setting::new("strength", "Glow strength", Control::Strength),
    Setting::new("brows", "Brows", Control::Toggle),
    Setting::new("mouth", "Mouth", Control::Toggle),
    Setting::new("effects", "Emotes", Control::Toggle),
    Setting::new("eye_details", "Eye details", Control::Toggle),
    Setting::new("mood_colour", "Mood colour", Control::Toggle),
];
const LAYOUT: &[Setting] = &[
    Setting::new(
        "menu_position",
        "Quick Menu",
        Control::Choices(&[
            ("left", "Left"),
            ("center", "Top centre"),
            ("right", "Right"),
            ("off", "Off"),
        ]),
    ),
    Setting::new("menu_labels", "Quick Menu labels", Control::Toggle),
    Setting::new(
        "dock",
        "Dock",
        Control::Choices(&[("always", "Always"), ("fades", "Fades"), ("off", "Off")]),
    ),
    Setting::new("clock_widget", "Clock widget", Control::Toggle),
    Setting::new(
        "clock_widget_position",
        "Clock corner",
        Control::Choices(CORNERS),
    ),
    Setting::new("system_widget", "System widget", Control::Toggle),
    Setting::new(
        "system_widget_position",
        "System corner",
        Control::Choices(CORNERS),
    ),
    Setting::new("weather_widget", "Weather widget", Control::Toggle),
    Setting::new(
        "weather_widget_position",
        "Weather corner",
        Control::Choices(CORNERS),
    ),
];
const VOICE: &[Setting] = &[
    Setting::new("captions", "Captions", Control::Toggle),
    Setting::new("caption_cost", "Caption cost badge", Control::Toggle),
    Setting::new("caption_heard", "Heard line", Control::Toggle),
];
const BEHAVIOUR: &[Setting] = &[Setting::new("idle_glances", "Idle glances", Control::Toggle)];
const SECTIONS: &[(&str, &[Setting])] = &[
    ("Look", LOOK),
    ("Layout", LAYOUT),
    ("Voice and text", VOICE),
    ("Behaviour", BEHAVIOUR),
    ("What can I say?", &[]),
];

pub fn searchable_settings() -> impl Iterator<Item = (&'static str, &'static str, &'static str)> {
    SECTIONS.iter().flat_map(|&(section, settings)| {
        settings.iter().map(move |setting| (setting.field, setting.title, section))
    })
}

#[derive(Default)]
pub struct Options {
    open: bool,
    from_menu: bool,
    section: usize,
    row: usize,
    help: Vec<(String, String)>,
    status: Option<String>,
    serial: u64,
}

impl Options {
    pub fn open(&self) -> bool {
        self.open
    }

    pub fn set_open(&mut self, open: bool) {
        self.open = open;
        if !open {
            self.from_menu = false;
            self.set_status(None);
        }
    }

    pub fn open_from_menu(&mut self) {
        self.set_open(true);
        self.set_status(None);
        self.from_menu = true;
    }

    pub fn opened_from_menu(&self) -> bool {
        self.from_menu
    }

    pub fn focus_setting(&mut self, field: &str) {
        for (section, (_, settings)) in SECTIONS.iter().enumerate() {
            if let Some(row) = settings.iter().position(|setting| setting.field == field) {
                self.section = section;
                self.row = row;
                self.serial = self.serial.wrapping_add(1);
                return;
            }
        }
    }

    pub fn style_key(&self) -> u64 {
        self.serial
    }

    pub fn show_help(&mut self) {
        self.section = SECTIONS.len() - 1;
        self.row = 0;
        self.serial = self.serial.wrapping_add(1);
    }

    pub fn set_status(&mut self, status: Option<String>) {
        let status = status
            .filter(|_| self.open)
            .map(|status| display_text(&status, 66))
            .filter(|status| !status.is_empty());
        if self.status != status {
            self.status = status;
            self.serial = self.serial.wrapping_add(1);
        }
    }

    pub fn set_help(&mut self, phrases: Vec<String>) {
        let phrases = phrases
            .into_iter()
            .filter_map(|line| {
                let (group, phrase) = line.split_once('|').unwrap_or(("", &line));
                let group = display_text(group, 20);
                let phrase = display_text(phrase, if group.is_empty() { 92 } else { 70 });
                (!phrase.is_empty()).then_some((group, phrase))
            })
            .take(96)
            .collect();
        if self.help != phrases {
            self.help = phrases;
            if self.settings().is_empty() {
                self.row = self.row.min(self.help.len().saturating_sub(HELP_ROWS));
            }
            self.serial = self.serial.wrapping_add(1);
        }
    }

    fn settings(&self) -> &'static [Setting] {
        SECTIONS[self.section].1
    }

    pub fn key(
        &mut self,
        key: Keycode,
        repeat: bool,
        prefs: Prefs,
        modifiers: Mod,
    ) -> Option<Change> {
        if !self.open {
            return None;
        }
        let before = (self.section, self.row);
        let help = self.settings().is_empty();
        let last = if help {
            self.help.len().saturating_sub(HELP_ROWS)
        } else {
            self.settings().len() - 1
        };
        match key {
            Keycode::Escape if !repeat => self.set_open(false),
            Keycode::Tab if !repeat => {
                let backwards = modifiers.intersects(Mod::LSHIFTMOD | Mod::RSHIFTMOD);
                let step = if backwards { SECTIONS.len() - 1 } else { 1 };
                self.section = (self.section + step) % SECTIONS.len();
                self.row = 0;
            }
            Keycode::Up => self.row = self.row.saturating_sub(1),
            Keycode::Down => self.row = (self.row + 1).min(last),
            Keycode::Home => self.row = 0,
            Keycode::End => self.row = last,
            Keycode::PageUp if help => self.row = self.row.saturating_sub(HELP_ROWS),
            Keycode::PageDown if help => self.row = (self.row + HELP_ROWS).min(last),
            Keycode::Left
            | Keycode::Right
            | Keycode::Return
            | Keycode::KpEnter
            | Keycode::Space
                if !repeat && !help =>
            {
                return self.adjust(key, prefs);
            }
            _ => {}
        }
        if before != (self.section, self.row) {
            self.serial = self.serial.wrapping_add(1);
        }
        None
    }

    fn adjust(&self, key: Keycode, prefs: Prefs) -> Option<Change> {
        let setting = self.settings().get(self.row)?;
        let applied = prefs.applied();
        let current = applied.data.get(setting.field).unwrap_or(&Value::Null);
        let previous = key == Keycode::Left;
        let next = key == Keycode::Right;
        let value = match setting.control {
            Control::Toggle => Value::Bool(if previous {
                false
            } else if next {
                true
            } else {
                !current.as_bool().unwrap_or(false)
            }),
            Control::Strength => {
                let percent = strength_percent(current);
                let step = if previous { -10 } else { 10 };
                json!(f64::from((percent + step).clamp(40, 160)) / 100.0)
            }
            Control::Palette => {
                let index = palette_index(current);
                Value::from(
                    Palette::NAMES[step_choice(index, Palette::NAMES.len(), previous, next)],
                )
            }
            Control::Choices(choices) => {
                let index = choice_index(choices, current);
                let wanted = step_choice(index, choices.len(), previous, next);
                Value::from(choices[wanted].0)
            }
        };
        (&value != current).then_some(Change {
            field: setting.field,
            value,
        })
    }

    /// Fit the card inside the panel below the eye strip.
    pub fn rect(panel: Rect, w: i32, h: i32) -> Rect {
        let scale = 1.15f32
            .min((panel.w - 32).max(1) as f32 / w as f32)
            .min((panel.h - 32).max(1) as f32 / h as f32);
        let w = ((w as f32 * scale).round() as i32).max(1);
        let h = ((h as f32 * scale).round() as i32).max(1);
        Rect {
            x: panel.x + (panel.w - w) / 2,
            y: panel.y + (panel.h - h) / 2,
            w,
            h,
        }
    }

    pub fn picture(&self, font: &Font, prefs: Prefs) -> Picture {
        let mut picture = chrome::picture(WIDTH as usize, HEIGHT as usize);
        picture.glass(rect(0, 0, WIDTH, HEIGHT), 18);
        font.draw(&mut picture, 24, 18, "Options", INK, 2);
        if let Some(status) = self.status.as_deref() {
            let x = WIDTH - 24 - status.chars().count() as i32 * font.w as i32;
            font.draw(&mut picture, x, 26, status, WARNING, 1);
        }
        let mut x = 24;
        for (index, (title, _)) in SECTIONS.iter().enumerate() {
            let width = title.len() as i32 * font.w as i32 + 28;
            let selected = index == self.section;
            pill(&mut picture, rect(x, 62, width, 28), selected);
            font.draw(
                &mut picture,
                x + 14,
                68,
                title,
                if selected { INK } else { DIM },
                1,
            );
            x += width + 8;
        }
        let settings = self.settings();
        let hint = if settings.is_empty() {
            self.draw_help(&mut picture, font);
            "Tab / Shift+Tab  Section    ↑ ↓ / PgUp / PgDn  Scroll    Esc  Back"
        } else {
            let applied = prefs.applied();
            let spacing = 42.min((FOOTER_TOP - SETTINGS_TOP) / settings.len() as i32);
            for (index, setting) in settings.iter().enumerate() {
                let y = SETTINGS_TOP + index as i32 * spacing;
                self.draw_row(
                    &mut picture,
                    font,
                    setting,
                    applied.data.get(setting.field),
                    y,
                    index == self.row,
                );
            }
            "Tab / Shift+Tab  Section    ↑ ↓  Setting    ← → / Enter  Change    Esc  Back"
        };
        picture.fill(rect(24, FOOTER_TOP, WIDTH - 48, 1), 0, EDGE);
        font.draw(&mut picture, 24, 468, hint, DIM, 1);
        picture
    }

    fn draw_help(&self, picture: &mut Picture, font: &Font) {
        if self.help.is_empty() {
            font.draw(
                picture,
                24,
                112,
                "Voice commands are unavailable right now.",
                DIM,
                1,
            );
            return;
        }
        font.draw(
            picture,
            24,
            106,
            "Say a phrase after your wake phrase.",
            DIM,
            1,
        );
        for (index, (group, phrase)) in self.help.iter().skip(self.row).take(HELP_ROWS).enumerate()
        {
            let y = 134 + index as i32 * 27;
            picture.fill(rect(24, y, WIDTH - 48, 24), 7, CHIP);
            let phrase_x = if group.is_empty() {
                34
            } else {
                font.draw(picture, 34, y + 4, group, DIM, 1);
                34 + 22 * font.w as i32
            };
            font.draw(picture, phrase_x, y + 4, phrase, INK, 1);
        }
        let range = format!(
            "{}–{} of {}",
            self.row + 1,
            (self.row + HELP_ROWS).min(self.help.len()),
            self.help.len()
        );
        font.draw(
            picture,
            WIDTH - 24 - range.chars().count() as i32 * font.w as i32,
            434,
            &range,
            DIM,
            1,
        );
    }

    fn draw_row(
        &self,
        picture: &mut Picture,
        font: &Font,
        setting: &Setting,
        value: Option<&Value>,
        y: i32,
        focused: bool,
    ) {
        let current = value.unwrap_or(&Value::Null);
        if focused {
            picture.fill(rect(20, y, WIDTH - 40, 38), 10, FOCUS);
            picture.fill(rect(26, y + 8, 2, 22), 1, GLOW);
        }
        font.draw(
            picture,
            38,
            y + 11,
            setting.title,
            if focused { INK } else { DIM },
            1,
        );
        match setting.control {
            Control::Toggle => {
                let on = current.as_bool().unwrap_or(false);
                picture.fill(
                    rect(738, y + 8, 52, 22),
                    11,
                    if on { SELECTED } else { EDGE },
                );
                picture.fill(
                    rect(if on { 768 } else { 742 }, y + 11, 16, 16),
                    8,
                    if on { GLOW } else { DIM },
                );
                font.draw(picture, 702, y + 11, if on { "On" } else { "Off" }, INK, 1);
            }
            Control::Strength => {
                let percent = strength_percent(current);
                let filled = (percent - 40) * 220 / 120;
                picture.fill(rect(512, y + 16, 220, 6), 3, EDGE);
                picture.fill(rect(512, y + 16, filled, 6), 3, SELECTED);
                picture.fill(rect(507 + filled, y + 14, 10, 10), 5, GLOW);
                font.draw(picture, 750, y + 11, &format!("{percent}%"), INK, 1);
            }
            Control::Palette => draw_palette(picture, font, y, palette_index(current)),
            Control::Choices(choices) => {
                let index = choice_index(choices, current);
                let mut right = 790;
                for (i, (_, label)) in choices.iter().enumerate().rev() {
                    let w = label.len() as i32 * font.w as i32 + 24;
                    let x = right - w;
                    let selected = i == index;
                    pill(picture, rect(x, y + 5, w, 28), selected);
                    font.draw(
                        picture,
                        x + 12,
                        y + 11,
                        label,
                        if selected { INK } else { DIM },
                        1,
                    );
                    right = x - 6;
                }
            }
        }
    }
}

fn display_text(text: &str, limit: usize) -> String {
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .filter(|ch| !ch.is_control())
        .take(limit)
        .collect()
}

fn step_choice(index: usize, len: usize, previous: bool, next: bool) -> usize {
    if previous {
        index.saturating_sub(1)
    } else if next {
        (index + 1).min(len - 1)
    } else {
        (index + 1) % len
    }
}

fn choice_index(choices: &[Choice], value: &Value) -> usize {
    choices
        .iter()
        .position(|(choice, _)| value.as_str() == Some(*choice))
        .unwrap_or(0)
}

fn palette_index(value: &Value) -> usize {
    Palette::NAMES
        .iter()
        .position(|name| value.as_str() == Some(*name))
        .unwrap_or(0)
}

fn draw_palette(picture: &mut Picture, font: &Font, y: i32, selected: usize) {
    let left = 796 - Palette::NAMES.len() as i32 * 32;
    let name = Palette::NAMES[selected];
    let title = format!("{}{}", name[..1].to_ascii_uppercase(), &name[1..]);
    font.draw(
        picture,
        left - 20 - title.len() as i32 * font.w as i32,
        y + 11,
        &title,
        INK,
        1,
    );
    for (index, name) in Palette::NAMES.iter().enumerate() {
        let mut look = render::Look::default();
        Palette::from_name(name)
            .unwrap_or_default()
            .apply(&mut look);
        let color = look.core.map(|channel| (channel * 255.0).round() as u8);
        let x = left + index as i32 * 32;
        let edge = if index == selected { GLOW } else { EDGE };
        picture.fill(rect(x, y + 6, 26, 26), 13, edge);
        picture.fill(rect(x + 2, y + 8, 22, 22), 11, CHIP);
        picture.fill(rect(x + 5, y + 11, 16, 16), 8, color);
    }
}

fn strength_percent(value: &Value) -> i32 {
    (value.as_f64().unwrap_or(1.0) * 100.0).round() as i32
}

fn rect(x: i32, y: i32, w: i32, h: i32) -> Rect {
    Rect { x, y, w, h }
}

fn pill(picture: &mut Picture, bounds: Rect, selected: bool) {
    picture.fill(bounds, 10, if selected { [41, 105, 123] } else { EDGE });
    picture.fill(
        rect(bounds.x + 1, bounds.y + 1, bounds.w - 2, bounds.h - 2),
        9,
        if selected { SELECTED } else { CHIP },
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shift_tab_moves_backwards_and_wraps() {
        let mut options = Options::default();
        options.set_open(true);
        let prefs = Prefs::default();
        options.key(Keycode::Tab, false, prefs, Mod::LSHIFTMOD);
        assert_eq!(SECTIONS[options.section].0, "What can I say?");
        options.key(Keycode::Tab, false, prefs, Mod::NOMOD);
        assert_eq!(SECTIONS[options.section].0, "Look");
        options.key(Keycode::Tab, false, prefs, Mod::NOMOD);
        options.key(Keycode::Down, false, prefs, Mod::NOMOD);
        options.key(Keycode::Tab, false, prefs, Mod::RSHIFTMOD);
        assert_eq!((SECTIONS[options.section].0, options.row), ("Look", 0));
    }

    #[test]
    fn focusing_a_search_result_opens_its_existing_control() {
        let mut options = Options::default();
        options.open_from_menu();
        options.focus_setting("captions");
        assert!(options.opened_from_menu());
        let prefs = Prefs::default();
        assert_eq!(
            options.key(Keycode::Return, false, prefs, Mod::NOMOD),
            Some(Change { field: "captions", value: Value::Bool(true) })
        );
        options.key(Keycode::Escape, false, prefs, Mod::NOMOD);
        assert!(!options.opened_from_menu());
    }

    #[test]
    fn keys_send_typed_changes_for_the_selected_setting() {
        let mut options = Options::default();
        let prefs = Prefs::default();
        assert_eq!(options.key(Keycode::Right, false, prefs, Mod::NOMOD), None);
        options.set_open(true);
        assert_eq!(
            options.key(Keycode::Right, false, prefs, Mod::NOMOD),
            Some(Change {
                field: "glow",
                value: Value::from(Palette::NAMES[1])
            })
        );
        assert_eq!(options.key(Keycode::Right, true, prefs, Mod::NOMOD), None);
        for _ in 0..3 {
            options.key(Keycode::Down, false, prefs, Mod::NOMOD);
        }
        assert_eq!(
            options.key(Keycode::Left, false, prefs, Mod::NOMOD),
            Some(Change {
                field: "strength",
                value: json!(0.9)
            })
        );
        options.key(Keycode::Tab, false, prefs, Mod::NOMOD);
        options.key(Keycode::Tab, false, prefs, Mod::NOMOD);
        assert_eq!(
            options.key(Keycode::Return, false, prefs, Mod::NOMOD),
            Some(Change {
                field: "captions",
                value: Value::Bool(true)
            })
        );
        options.key(Keycode::Escape, false, prefs, Mod::NOMOD);
        assert!(!options.open());
    }

    #[test]
    fn every_eye_colour_is_reachable_and_the_control_cycles() {
        let mut options = Options::default();
        options.set_open(true);
        let mut prefs = Prefs::default();
        for &name in &Palette::NAMES[1..] {
            let change = options.key(Keycode::Right, false, prefs, Mod::NOMOD).unwrap();
            assert_eq!(change.field, "glow");
            assert_eq!(change.value, name);
            prefs = Prefs::requested(&bus::Event::new("screen.prefs").with("glow", name));
        }
        assert_eq!(options.key(Keycode::Right, false, prefs, Mod::NOMOD), None);
        let wrapped = options.key(Keycode::Return, false, prefs, Mod::NOMOD).unwrap();
        assert_eq!(wrapped.value, Palette::NAMES[0]);
    }

    #[test]
    fn emotes_eye_details_and_mood_colour_are_separate_options() {
        let mut options = Options::default();
        options.set_open(true);
        let prefs = Prefs::default();
        for _ in 0..6 {
            options.key(Keycode::Down, false, prefs, Mod::NOMOD);
        }
        for field in ["effects", "eye_details", "mood_colour"] {
            assert_eq!(
                options.key(Keycode::Return, false, prefs, Mod::NOMOD),
                Some(Change {
                    field,
                    value: Value::Bool(true),
                })
            );
            options.key(Keycode::Down, false, prefs, Mod::NOMOD);
        }
    }

    #[test]
    fn help_scrolls_and_resets_when_the_registry_list_shrinks() {
        let mut options = Options::default();
        let prefs = Prefs::default();
        options.set_open(true);
        for _ in 0..SECTIONS.len() - 1 {
            options.key(Keycode::Tab, false, prefs, Mod::NOMOD);
        }
        options.set_help(
            (0..30)
                .map(|i| format!("Appearance|Show example {i}"))
                .collect(),
        );
        options.key(Keycode::PageDown, false, prefs, Mod::NOMOD);
        assert_eq!(options.row, HELP_ROWS);
        assert_eq!(&options.help[0].0, "Appearance");
        options.set_help(vec!["Appearance|Make your face smaller".into()]);
        assert_eq!(options.row, 0);
    }
}

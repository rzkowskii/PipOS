//! The Quick Menu shares the workshop dock, with one keyboard owner at a time.

use crate::{
    chrome::{self, DIM, EDGE, FAINT, GLOW, INK, WARNING},
    panel::Rect,
    text::{Font, Picture},
};
use sdl2::keyboard::Keycode;
use std::time::{Duration, Instant};

// Allow the mind's seven-second app-open request to resolve before the menu gives up.
const OPEN_TIMEOUT: Duration = Duration::from_secs(8);
const QUERY_LIMIT: usize = 48;
const LIST_ROWS: usize = 12;

const APPS: &[(&str, &str)] = &[
    ("Terminal", "app.terminal.open"),
    ("Monitor", "app.monitor.open"),
    ("Clock", "app.clock.open"),
    ("Calculator", "app.calc.open"),
    ("Lists", "app.lists.open"),
    ("Memory", "app.memory.open"),
    ("Journal", "app.journal.open"),
    ("Mind", "app.mind.open"),
];
const LOOKUP: (&str, &str) = ("Look it up", "app.lookup.open");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Selection {
    App(&'static str),
    Setting(&'static str),
    Options,
    Help,
    Care,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum View {
    Grid,
    List,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct StyleKey {
    selected: usize,
    options: bool,
    pub(crate) handoff: Handoff,
    online: bool,
    position: Position,
    care: bool,
    view: View,
    searching: bool,
    query_revision: u64,
}

struct Entry {
    label: &'static str,
    id: &'static str,
    section: &'static str,
    selection: Selection,
}

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub enum Handoff {
    #[default]
    Ready,
    Waiting { app: &'static str, until: Instant },
    Refused,
    TimedOut,
}

#[derive(Clone, Copy, Default, Debug, PartialEq, Eq)]
pub enum Position {
    Left,
    #[default]
    Center,
    Right,
}

impl Position {
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "left" => Some(Self::Left),
            "center" => Some(Self::Center),
            "right" => Some(Self::Right),
            _ => None,
        }
    }

    /// Fit the rail below the eyes, or alongside the centre of the surface.
    pub fn rect(self, panel: Rect, w: i32, h: i32) -> Rect {
        let margin = (panel.w / 44).clamp(4, 32);
        let scale = (panel.w as f32 / 1100.0).clamp(1.0, 1.6)
            .min((panel.w - 2 * margin).max(1) as f32 / w as f32)
            .min((panel.h - 32).max(1) as f32 / h as f32);
        let w = ((w as f32 * scale).round() as i32).max(1);
        let h = ((h as f32 * scale).round() as i32).max(1);
        let x = match self {
            Self::Left => margin,
            Self::Center => (panel.w - w) / 2,
            Self::Right => panel.w - w - margin,
        };
        let y = match self {
            Self::Center => (panel.h - h - margin).max(0),
            _ => (panel.h - h) / 2,
        };
        Rect { x: panel.x + x, y: panel.y + y, w, h }
    }
}

#[derive(Default)]
pub struct Menu {
    open: bool,
    guest: bool,
    online: bool,
    care_allowed: bool,
    selected: usize,
    handoff: Handoff,
    reaction: Option<bool>,
    pub position: Position,
    view: Option<View>,
    query: String,
    search: bool,
    query_revision: u64,
}

impl Menu {
    pub fn open(&self) -> bool {
        self.open
    }

    pub fn guest(&self) -> bool {
        self.guest
    }

    pub fn searching(&self) -> bool {
        self.open && self.search
    }

    /// Feed SDL TextInput while the menu owns the keyboard.
    pub fn input(&mut self, text: &str) {
        if !self.searching() || matches!(self.handoff, Handoff::Waiting { .. }) {
            return;
        }
        let before = self.query.len();
        self.query.extend(text.chars()
            .filter(|&ch| ch.is_ascii_alphanumeric() || matches!(ch, ' ' | '-' | '_' | '?'))
            .take(QUERY_LIMIT - before));
        if self.query.len() != before {
            self.query_changed();
        }
    }

    fn query_changed(&mut self) {
        self.selected = 0;
        self.handoff = Handoff::Ready;
        self.reaction = None;
        self.query_revision = self.query_revision.wrapping_add(1);
    }

    fn view(&self) -> View {
        self.view.unwrap_or(match self.position {
            Position::Center => View::Grid,
            Position::Left | Position::Right => View::List,
        })
    }

    pub fn set_open(&mut self, requested: bool) {
        let open = requested && !self.guest;
        if open && !self.open {
            self.selected = 0;
            self.reaction = None;
        }
        if !open {
            self.handoff = Handoff::Ready;
            self.reaction = None;
            self.search = false;
            self.query.clear();
        }
        self.open = open;
    }

    pub fn privacy(&mut self, guest: bool) {
        self.guest = guest;
        self.set_open(false);
    }

    pub fn set_online(&mut self, allowed: bool) {
        if self.online == allowed {
            return;
        }
        self.online = allowed;
        if !self.searching() && self.selected >= APPS.len() {
            if allowed {
                self.selected += 1;
            } else {
                self.selected -= 1;
            }
        }
        if !allowed {
            self.refused("lookup");
        }
    }

    fn apps(&self) -> impl Iterator<Item = (&'static str, &'static str)> {
        APPS.iter().copied().chain(self.online.then_some(LOOKUP))
    }

    pub fn set_care_allowed(&mut self, allowed: bool) {
        self.care_allowed = allowed;
    }

    pub fn style_key(&self, options: bool) -> StyleKey {
        StyleKey {
            selected: self.selected,
            options,
            handoff: self.handoff,
            online: self.online,
            position: self.position,
            care: self.care_allowed,
            view: self.view(),
            searching: self.searching(),
            query_revision: self.query_revision,
        }
    }

    fn shortcuts(&self, enabled: bool) -> impl Iterator<Item = (&'static str, &'static str, Selection)> {
        (enabled && self.care_allowed).then_some(("Care", "care", Selection::Care))
            .into_iter()
            .chain(enabled.then_some(("What can I say?", "help", Selection::Help)))
            .chain(enabled.then_some(("Options", "options", Selection::Options)))
    }

    fn entries(&self, options: bool) -> Vec<Entry> {
        let mut entries: Vec<_> = self.apps().map(|(label, id)| Entry {
            label, id, section: "", selection: Selection::App(id),
        }).collect();
        entries.extend(self.shortcuts(options).map(|(label, id, selection)| Entry {
            label, id, section: "", selection,
        }));
        let query = self.query.trim().replace('_', " ").to_ascii_lowercase();
        if query.is_empty() {
            return entries;
        }
        if options {
            entries.extend(crate::options::searchable_settings().map(|(id, label, section)| Entry {
                label, id, section, selection: Selection::Setting(id),
            }));
        }
        entries.retain(|entry| {
            let aliases = match entry.id {
                "app.calc.open" => "calc arithmetic",
                "app.lists.open" => "tasks checklist",
                "app.memory.open" => "notes",
                "app.journal.open" => "diary",
                "app.mind.open" => "focus",
                "app.lookup.open" => "search web internet wikipedia",
                "options" => "settings preferences",
                "help" => "voice commands",
                "glow" => "color palette",
                _ => "",
            };
            let terms = format!("{} {} {} {}", entry.label, entry.id.replace('_', " "), entry.section, aliases)
                .to_ascii_lowercase();
            query.split_whitespace().all(|word| terms.contains(word))
        });
        entries
    }

    pub fn key(&mut self, key: Keycode, repeat: bool, options: bool, now: Instant) -> Option<Selection> {
        if !self.open {
            return None;
        }
        if key == Keycode::Escape && !repeat {
            if self.searching() && !matches!(self.handoff, Handoff::Waiting { .. }) {
                self.search = false;
                self.query.clear();
                self.query_changed();
            } else {
                self.set_open(false);
            }
            return None;
        }
        if matches!(self.handoff, Handoff::Waiting { .. }) {
            return None;
        }
        match key {
            Keycode::Slash if !repeat && !self.searching() => {
                self.search = true;
                self.query_changed();
                return None;
            }
            Keycode::Backspace if self.searching() => {
                if self.query.pop().is_some() {
                    self.query_changed();
                }
                return None;
            }
            Keycode::Tab if !repeat && !self.searching() => {
                self.view = Some(match self.view() {
                    View::Grid => View::List,
                    View::List => View::Grid,
                });
                return None;
            }
            _ => {}
        }
        let grid = !self.searching() && self.view() == View::Grid;
        let activate = !repeat && (matches!(key, Keycode::Return | Keycode::KpEnter)
            || (key == Keycode::Space && !self.searching()));
        let navigate = matches!(key, Keycode::Up | Keycode::Down | Keycode::Home | Keycode::End)
            || (grid && matches!(key, Keycode::Left | Keycode::Right));
        if !activate && !navigate {
            return None;
        }
        let entries = self.entries(options);
        let count = entries.len();
        if count == 0 {
            return None;
        }
        self.selected = self.selected.min(count - 1);
        self.reaction = None;
        if activate {
            let selection = entries[self.selected].selection;
            if let Selection::App(id) = selection {
                let app = id.strip_prefix("app.").and_then(|route| route.strip_suffix(".open"))?;
                self.handoff = Handoff::Waiting { app, until: now + OPEN_TIMEOUT };
            } else {
                self.set_open(false);
            }
            return Some(selection);
        }
        match key {
            Keycode::Up | Keycode::Left => self.selected = (self.selected + count - 1) % count,
            Keycode::Down | Keycode::Right => self.selected = (self.selected + 1) % count,
            Keycode::Home => self.selected = 0,
            Keycode::End => self.selected = count - 1,
            _ => return None,
        }
        self.handoff = Handoff::Ready;
        None
    }

    pub fn refused(&mut self, app: &str) {
        if matches!(self.handoff, Handoff::Waiting { app: expected, .. } if app == expected) {
            self.handoff = Handoff::Refused;
            self.reaction = Some(false);
        }
    }

    /// Acknowledgement alone is not focus. Follow the actual visible
    /// frame's owner before yielding the keyboard to the workshop again.
    pub fn observe(&mut self, visible_app: Option<&str>, now: Instant) {
        if let Handoff::Waiting { app, until } = self.handoff {
            if visible_app == Some(app) {
                self.set_open(false);
                self.reaction = Some(true);
            } else if now >= until {
                self.handoff = Handoff::TimedOut;
                self.reaction = Some(false);
            }
        }
    }

    pub fn waiting(&self) -> bool {
        self.open && matches!(self.handoff, Handoff::Waiting { .. })
    }

    /// The pending handoff has resolved. Subsequent frames do not play it again.
    pub fn take_reaction(&mut self) -> Option<bool> {
        self.reaction.take()
    }

    pub fn picture(&self, font: &Font, options: bool, labels: bool) -> Picture {
        let entries = self.entries(options);
        if self.searching() || self.view() == View::List {
            return self.list_picture(font, &entries);
        }

        let columns = if labels { entries.len().div_ceil(2) } else { entries.len() };
        let (w, h) = if labels { (24 + 136 * columns, 212) } else { (24 + 48 * columns, 112) };
        let mut picture = chrome::picture(w, h);
        let (w, h) = (w as i32, h as i32);
        let glyph = font.w as i32;
        picture.glass(Rect { x: 0, y: 0, w, h }, 18);
        font.draw(&mut picture, 16, 11, "Quick Menu", INK, 1);

        for (index, item) in entries.iter().enumerate() {
            let (x, y, row_w, row_h) = if labels {
                (12 + (index % columns) as i32 * 136, 34 + (index / columns) as i32 * 57, 130, 52)
            } else {
                (12 + index as i32 * 48, 34, 44, 34)
            };
            let focused = index == self.selected;
            if focused {
                picture.fill(Rect { x, y, w: row_w, h: row_h }, 11, GLOW);
                picture.fill(Rect { x: x + 1, y: y + 1, w: row_w - 2, h: row_h - 2 },
                             10, [17, 46, 57]);
            }
            icon(&mut picture, x + (row_w - 24) / 2, y + 4, item.id, if focused { GLOW } else { DIM });
            if labels {
                let label_x = x + (row_w - item.label.len() as i32 * glyph).max(0) / 2;
                font.draw(&mut picture, label_x, y + 29, item.label, if focused { INK } else { DIM }, 1);
            }
        }
        let footer = h - 34;
        picture.fill(Rect { x: 16, y: footer, w: w - 32, h: 1 }, 0, EDGE);
        let (status, color) = self.handoff_status();
        let hint = if labels { "Arrows   Enter Open   Tab List   / Search   Esc Close" }
            else { "Enter   Tab List   /   Esc" };
        font.draw(&mut picture, 16, footer + 8, status.unwrap_or(hint), color, 1);
        if !labels {
            if let Some(item) = entries.get(self.selected) {
                font.draw(&mut picture, w - 16 - item.label.len() as i32 * glyph, footer + 8, item.label,
                          if status.is_none() { INK } else { FAINT }, 1);
            }
        }
        picture
    }

    fn handoff_status(&self) -> (Option<&'static str>, chrome::Color) {
        match self.handoff {
            Handoff::Ready => (None, DIM),
            Handoff::Waiting { .. } => (Some("Opening…"), GLOW),
            Handoff::Refused => (Some("Could not open"), WARNING),
            Handoff::TimedOut => (Some("App timed out"), WARNING),
        }
    }

    fn list_picture(&self, font: &Font, entries: &[Entry]) -> Picture {
        const WIDTH: usize = 780;
        const HEIGHT: usize = 526;
        const DIVIDER: i32 = 277;
        const ROW_TOP: i32 = 89;
        const FOOTER: i32 = 480;
        let mut picture = chrome::picture(WIDTH, HEIGHT);
        let w = WIDTH as i32;
        picture.glass(Rect { x: 0, y: 0, w, h: HEIGHT as i32 }, 18);
        font.draw(&mut picture, 18, 14, "Quick Menu", INK, 2);

        let searching = self.searching();
        let search_width = w - 166;
        let search_box = Rect { x: 18, y: 55, w: search_width, h: 24 };
        picture.fill(search_box, 6, if searching { GLOW } else { EDGE });
        picture.fill(Rect { x: search_box.x + 1, y: search_box.y + 1, w: search_box.w - 2, h: 22 },
                     5, [12, 24, 33]);
        if self.query.is_empty() {
            font.draw(&mut picture, 27, 59, "/  Find apps or settings", DIM, 1);
        } else {
            font.draw(&mut picture, 27, 59, &self.query, INK, 1);
        }
        picture.fill(Rect { x: w - 138, y: 55, w: 120, h: 24 }, 6, EDGE);
        picture.fill(Rect { x: w - 79, y: 56, w: 60, h: 22 }, 5, [17, 46, 57]);
        font.draw(&mut picture, w - 124, 59, "Grid", DIM, 1);
        font.draw(&mut picture, w - 65, 59, "List", GLOW, 1);

        let selected = self.selected.min(entries.len().saturating_sub(1));
        let offset = selected.saturating_sub(LIST_ROWS - 1);
        let items = &entries[offset..entries.len().min(offset + LIST_ROWS)];
        let glyph = font.w as i32;
        let mut group = None;
        let mut y = ROW_TOP;
        for (index, item) in items.iter().enumerate() {
            let heading = if searching {
                "Results"
            } else if matches!(item.selection, Selection::App(_)) {
                "Apps"
            } else {
                "Pip"
            };
            if group != Some(heading) {
                font.draw(&mut picture, 19, y, heading, DIM, 1);
                y += 20;
                group = Some(heading);
            }
            let focused = index + offset == selected;
            if focused {
                picture.fill(Rect { x: 16, y, w: 244, h: 27 }, 7, GLOW);
                picture.fill(Rect { x: 17, y: y + 1, w: 242, h: 25 }, 6, [17, 46, 57]);
            }
            let id = if item.section.is_empty() { item.id } else { "options" };
            icon(&mut picture, 24, y + 1, id, if focused { GLOW } else { DIM });
            font.draw(&mut picture, 55, y + 6, item.label, if focused { INK } else { DIM }, 1);
            y += 29;
        }
        picture.fill(Rect { x: DIVIDER, y: ROW_TOP, w: 1, h: FOOTER - ROW_TOP }, 0, EDGE);
        picture.fill(Rect { x: 18, y: FOOTER, w: w - 36, h: 1 }, 0, EDGE);

        let detail_x = DIVIDER + 34;
        let (status, color) = self.handoff_status();
        if let Some(item) = entries.get(selected) {
            let settings = !item.section.is_empty();
            icon(&mut picture, detail_x, 170, if settings { "options" } else { item.id }, GLOW);
            font.draw(&mut picture, detail_x, 210, item.label, INK, 2);
            if settings {
                font.draw(&mut picture, detail_x, 256, &format!("Options  /  {}", item.section), DIM, 1);
            } else {
                for (row, line) in description(item.id).iter().enumerate() {
                    font.draw(&mut picture, detail_x, 256 + row as i32 * 20, line, DIM, 1);
                }
            }
            if let Some(status) = status {
                font.draw(&mut picture, detail_x, 317, status, color, 1);
            } else {
                let action = if settings { "Enter  Open setting" } else { "Enter  Open" };
                picture.fill(Rect { x: detail_x, y: 310, w: action.len() as i32 * glyph + 24, h: 30 },
                             6, [17, 46, 57]);
                font.draw(&mut picture, detail_x + 12, 317, action, GLOW, 1);
            }
        } else {
            font.draw(&mut picture, detail_x, 210, "No matches", INK, 2);
            font.draw(&mut picture, detail_x, 256, "Try an app or a setting such as eye colour.", DIM, 1);
        }

        let hint = if searching { "Arrows Move   Esc Back" } else { "Arrows Move   Tab Grid   / Search   Esc Close" };
        font.draw(&mut picture, 18, FOOTER + 13, hint, DIM, 1);
        if entries.len() > items.len() {
            let count = format!("{}/{}", selected + 1, entries.len());
            font.draw(&mut picture, w - 18 - count.len() as i32 * glyph, FOOTER + 13, &count, DIM, 1);
        }
        picture
    }
}

fn description(id: &str) -> &'static [&'static str] {
    match id {
        "app.terminal.open" => &["Use the local workshop shell and built-in tools."],
        "app.monitor.open" => &["See battery, temperatures, processor load", "and running services."],
        "app.clock.open" => &["See local time, upcoming alarms and timers."],
        "app.calc.open" => &["Type a calculation and press Enter."],
        "app.lists.open" => &["Read your saved lists and their items."],
        "app.memory.open" => &["Read your latest saved facts."],
        "app.journal.open" => &["Review recent activity, dreams and last night."],
        "app.mind.open" => &["See how Pip routed recent requests."],
        "app.lookup.open" => &["Find a cited result with Internet and web", "lookup enabled."],
        "options" => &["Change Pip's look, captions and widgets."],
        "help" => &["Browse examples of things you can say to Pip."],
        "care" => &["Manage senses, memory, connectivity and backups."],
        _ => &[],
    }
}

fn icon(picture: &mut Picture, x: i32, y: i32, id: &str, color: chrome::Color) {
    let mut path = |points: &[(f32, f32)]| picture.path(x, y, points, color);
    match id {
        "app.terminal.open" => {
            path(&[(3., 5.), (21., 5.), (21., 21.), (3., 21.), (3., 5.)]);
            path(&[(7., 10.), (10., 13.), (7., 16.)]);
            path(&[(13., 16.), (17., 16.)]);
        }
        "app.monitor.open" => {
            path(&[(3., 5.), (21., 5.), (21., 18.), (3., 18.), (3., 5.)]);
            path(&[(6., 14.), (9., 11.), (12., 13.), (17., 9.)]);
            path(&[(8., 22.), (16., 22.)]);
        }
        "app.clock.open" => {
            path(&[(8., 5.), (16., 5.), (21., 10.), (21., 16.), (16., 21.), (8., 21.),
                   (3., 16.), (3., 10.), (8., 5.)]);
            path(&[(12., 8.), (12., 13.), (16., 15.)]);
        }
        "app.calc.open" => {
            path(&[(6., 4.), (18., 4.), (18., 22.), (6., 22.), (6., 4.)]);
            path(&[(9., 8.), (15., 8.)]);
            for y in [12., 16., 19.] {
                for x in [9., 12., 15.] {
                    path(&[(x, y), (x, y)]);
                }
            }
        }
        "app.lists.open" => {
            for y in [7., 13., 19.] {
                path(&[(3., y), (5., y + 2.), (7., y - 1.)]);
                path(&[(11., y), (20., y)]);
            }
        }
        "app.memory.open" => {
            path(&[(3., 9.), (12., 4.), (21., 9.), (12., 14.), (3., 9.)]);
            path(&[(3., 13.), (12., 18.), (21., 13.)]);
            path(&[(3., 17.), (12., 22.), (21., 17.)]);
        }
        "app.journal.open" => path(&[(10., 4.), (5., 7.), (3., 12.), (4., 17.), (8., 21.),
            (14., 22.), (19., 19.), (21., 15.), (16., 16.), (11., 13.), (9., 9.), (10., 4.)]),
        "app.mind.open" => {
            path(&[(8., 13.), (12., 9.), (16., 13.), (12., 17.), (8., 13.)]);
            for (x, y, cx, cy) in [(4., 6., 10., 11.), (20., 6., 14., 11.), (4., 20., 10., 15.), (20., 20., 14., 15.)] {
                path(&[(cx, cy), (x, y)]);
                path(&[(x - 1., y), (x, y - 1.), (x + 1., y), (x, y + 1.), (x - 1., y)]);
            }
        }
        "app.lookup.open" => {
            path(&[(5., 6.), (13., 6.), (16., 9.), (16., 15.), (13., 18.), (5., 18.), (2., 15.), (2., 9.), (5., 6.)]);
            path(&[(14., 17.), (21., 23.)]);
        }
        "care" => {
            path(&[(12., 4.), (20., 7.), (20., 14.), (17., 19.), (12., 22.), (7., 19.), (4., 14.), (4., 7.), (12., 4.)]);
            path(&[(8., 13.), (11., 16.), (16., 10.)]);
        }
        "help" => {
            path(&[(6., 10.), (8., 6.), (12., 4.), (16., 5.), (18., 8.), (17., 11.), (12., 14.), (12., 16.)]);
            path(&[(12., 20.), (12., 20.)]);
        }
        _ => {
            path(&[(4., 8.), (13., 8.)]);
            path(&[(13., 8.), (16., 5.), (19., 8.), (16., 11.), (13., 8.)]);
            path(&[(19., 8.), (21., 8.)]);
            path(&[(4., 18.), (6., 18.)]);
            path(&[(6., 18.), (9., 15.), (12., 18.), (9., 21.), (6., 18.)]);
            path(&[(12., 18.), (21., 18.)]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn list_details_follow_focus_and_share_the_search_layout() {
        let font = crate::text::font().unwrap();
        let mut menu = Menu::default();
        let now = Instant::now();
        menu.set_care_allowed(true);
        menu.set_open(true);
        let grid = menu.picture(font, true, true);
        menu.key(Keycode::Tab, false, true, now);
        menu.key(Keycode::End, false, true, now);
        assert_eq!(menu.entries(true)[menu.selected].id, "options");
        let list = menu.picture(font, true, false);
        assert_eq!((list.w, list.h), (780, 526));
        assert!(list.rgba == menu.picture(font, true, true).rgba);

        menu.key(Keycode::Slash, false, true, now);
        menu.input("eye colour");
        let search = menu.picture(font, true, false);
        assert_eq!((search.w, search.h), (list.w, list.h));
        assert!((210..242).any(|y| {
            let detail = (list.w * y + 311) * 4..list.w * (y + 1) * 4;
            list.rgba[detail.clone()] != search.rgba[detail]
        }));
        assert_eq!(menu.entries(true)[menu.selected].selection, Selection::Setting("glow"));

        menu.input(" no match");
        assert!(menu.entries(true).is_empty());
        let empty = menu.picture(font, true, false);
        assert_eq!((empty.w, empty.h), (list.w, list.h));

        if let Some(folder) = std::env::var_os("BEING_GUI_CAPTURE_DIR").map(std::path::PathBuf::from) {
            assert!(folder.is_dir());
            for (name, picture) in [("grid", grid), ("list-options", list),
                                    ("search-setting", search), ("search-empty", empty)] {
                crate::write_png(&folder.join(format!("{name}.png")), picture.w as u32,
                                 picture.h as u32, &picture.rgba).unwrap();
            }
        }
    }

    #[test]
    fn search_edits_multiword_queries_and_selects_the_matching_setting() {
        let now = Instant::now();
        let mut menu = Menu::default();
        menu.set_open(true);
        let before = menu.style_key(true);
        menu.key(Keycode::Slash, false, true, now);
        assert!(menu.searching());
        assert_ne!(menu.style_key(true), before);
        menu.input("/Eye");
        assert_eq!(menu.key(Keycode::Space, false, true, now), None);
        menu.input(" detailx");
        menu.key(Keycode::Backspace, false, true, now);
        assert_eq!(menu.query, "Eye detail");
        assert_eq!(menu.key(Keycode::Return, false, true, now), Some(Selection::Setting("eye_details")));
        assert!(!menu.open());
    }

    #[test]
    fn search_honours_lookup_permission_and_the_app_handoff() {
        let now = Instant::now();
        let mut menu = Menu::default();
        menu.set_open(true);
        menu.key(Keycode::Slash, false, true, now);
        menu.input("Wikipedia");
        assert_eq!(menu.key(Keycode::Return, false, true, now), None);
        menu.set_online(true);
        assert_eq!(menu.key(Keycode::Return, false, true, now), Some(Selection::App("app.lookup.open")));
        menu.input(" extra text");
        assert_eq!(menu.query, "Wikipedia");
        assert!(matches!(menu.handoff, Handoff::Waiting { app: "lookup", .. }));
        menu.privacy(true);
        assert!(!menu.searching());
        assert!(menu.query.is_empty());
    }

    #[test]
    fn side_list_can_switch_to_grid_and_escape_leaves_search_first() {
        let now = Instant::now();
        let mut menu = Menu { position: Position::Left, ..Menu::default() };
        menu.set_open(true);
        assert_eq!(menu.view(), View::List);
        menu.key(Keycode::Tab, false, true, now);
        assert_eq!(menu.view(), View::Grid);
        menu.key(Keycode::Slash, false, true, now);
        menu.input(&"a".repeat(QUERY_LIMIT * 2));
        assert_eq!(menu.query.len(), QUERY_LIMIT);
        menu.key(Keycode::Escape, false, true, now);
        assert!(menu.open());
        assert!(!menu.searching());
        menu.key(Keycode::Escape, false, true, now);
        menu.set_open(true);
        assert_eq!(menu.view(), View::Grid);
    }

    #[test]
    fn keyboard_wraps_selects_once_and_opens_existing_routes() {
        let now = Instant::now();
        let mut menu = Menu::default();
        assert!(menu.key(Keycode::Return, false, true, now).is_none());
        menu.set_open(true);
        menu.key(Keycode::Up, false, true, now);
        assert_eq!(menu.selected, APPS.len() + 1);
        assert!(menu.key(Keycode::Return, true, true, now).is_none());
        assert_eq!(menu.key(Keycode::Return, false, true, now), Some(Selection::Options));
        assert!(!menu.open());
        for (index, &(_, id)) in APPS.iter().enumerate() {
            menu.set_open(true);
            for _ in 0..index {
                menu.key(Keycode::Down, false, false, now);
            }
            assert_eq!(menu.key(Keycode::Return, false, false, now), Some(Selection::App(id)));
            let app = id.trim_start_matches("app.").trim_end_matches(".open");
            menu.observe(Some(app), now);
            assert!(!menu.open());
        }
        menu.set_open(true);
        menu.key(Keycode::Escape, false, false, now);
        assert!(!menu.open());
    }

    #[test]
    fn a_selection_holds_focus_over_the_previous_app_until_the_target_frame() {
        let now = Instant::now();
        let mut menu = Menu::default();
        menu.set_open(true);
        menu.key(Keycode::Slash, false, false, now);
        menu.input("lists");
        assert_eq!(menu.key(Keycode::Return, false, false, now), Some(Selection::App("app.lists.open")));
        menu.observe(Some("terminal"), now + Duration::from_secs(3));
        assert!(menu.waiting());
        assert!(menu.take_reaction().is_none());
        assert!(menu.key(Keycode::Return, false, false, now).is_none());
        menu.observe(Some("lists"), now + Duration::from_secs(3));
        assert!(!menu.open());
        assert_eq!(menu.take_reaction(), Some(true));
        menu.observe(Some("lists"), now + Duration::from_secs(4));
        assert!(menu.take_reaction().is_none());
    }

    #[test]
    fn refusal_keeps_focus_and_ignores_an_unrelated_apps_reply() {
        let now = Instant::now();
        let mut menu = Menu::default();
        menu.set_open(true);
        menu.key(Keycode::Return, false, false, now);
        menu.refused("memory");
        assert!(matches!(menu.handoff, Handoff::Waiting { app: "terminal", .. }));
        menu.refused("terminal");
        assert_eq!(menu.handoff, Handoff::Refused);
        assert_eq!(menu.take_reaction(), Some(false));
        assert!(menu.take_reaction().is_none());
        assert!(menu.open());
        assert_eq!(menu.key(Keycode::Return, false, false, now), Some(Selection::App("app.terminal.open")));
    }

    #[test]
    fn timeout_keeps_the_menu_in_control_until_explicit_dismissal() {
        let now = Instant::now();
        let mut menu = Menu::default();
        menu.set_open(true);
        menu.key(Keycode::Return, false, false, now);
        menu.observe(Some("lists"), now + OPEN_TIMEOUT);
        assert_eq!(menu.handoff, Handoff::TimedOut);
        assert_eq!(menu.take_reaction(), Some(false));
        assert!(menu.open());
        menu.observe(Some("terminal"), now + OPEN_TIMEOUT);
        assert!(menu.open(), "a late arrival cannot silently change keyboard ownership");
        assert!(menu.take_reaction().is_none());
        menu.key(Keycode::Escape, false, false, now + OPEN_TIMEOUT);
        assert!(!menu.open());
    }

    #[test]
    fn guest_mode_closes_the_menu_and_prevents_opening() {
        let mut menu = Menu::default();
        menu.set_open(true);
        menu.privacy(true);
        menu.set_open(true);
        assert!(!menu.open());
        menu.privacy(false);
        menu.set_open(true);
        assert!(menu.open());
    }

    #[test]
    fn alignment_moves_one_menu_without_moving_the_dock() {
        let panel = Rect { x: 0, y: 0, w: 1280, h: 640 };
        let [left, middle, right] = [Position::Left, Position::Center, Position::Right]
            .map(|position| position.rect(panel, 232, 128));
        assert!(left.x < middle.x && middle.x < right.x);
        assert_eq!([left.y, left.w, left.h], [right.y, right.w, right.h]);
        assert!(right.x + right.w <= panel.w);
    }
}

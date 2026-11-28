//! The display-side view of the owner's saved appearance.

use crate::{colour::Palette, panel, render};
use bus::Event;
use serde_json::{Map, Value};
use std::io::Read;
use std::path::Path;

pub const MIRROR: &str = "/run/being/look.json";
const MAX_BYTES: usize = 4096;
const CORNERS: [&str; 4] = ["top-left", "top-right", "bottom-left", "bottom-right"];

#[derive(Clone, Copy)]
pub enum Corner {
    TopLeft = 0,
    TopRight = 1,
    BottomLeft = 2,
    BottomRight = 3,
}

impl Corner {
    fn from_name(name: &str) -> Self {
        match name {
            "top-right" => Self::TopRight,
            "bottom-left" => Self::BottomLeft,
            "bottom-right" => Self::BottomRight,
            _ => Self::TopLeft,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Prefs {
    glow: &'static str,
    shape: &'static str,
    pub strength: f64,
    size: &'static str,
    pub brows: bool,
    pub mouth: bool,
    pub effects: bool,
    pub eye_details: bool,
    pub mood_colour: bool,
    pub clock_widget: bool,
    pub system_widget: bool,
    pub weather_widget: bool,
    pub menu_position: &'static str,
    pub menu_labels: bool,
    clock_widget_position: &'static str,
    system_widget_position: &'static str,
    weather_widget_position: &'static str,
    pub captions: bool,
    pub caption_cost: bool,
    pub caption_heard: bool,
    pub idle_glances: bool,
    pub revision: u64,
}

impl Default for Prefs {
    fn default() -> Self {
        Self { glow: "cyan", shape: "soft", strength: 1.0, size: "full", brows: false, mouth: false,
               effects: false, eye_details: false, mood_colour: false,
               clock_widget: false, system_widget: false, weather_widget: false, revision: 0,
               menu_position: "center", menu_labels: false, clock_widget_position: "top-left",
               system_widget_position: "top-right", weather_widget_position: "top-right",
               captions: false, caption_cost: true, caption_heard: true, idle_glances: true }
    }
}

impl Prefs {
    pub fn startup(state: &Path, mirror: &Path) -> Self {
        Self::read(state).or_else(|| Self::read(mirror)).unwrap_or_default()
    }

    fn decode(fields: &Map<String, Value>) -> Self {
        let defaults = Self::default();
        let choice = |key: &str, choices: &[&'static str], fallback| {
            let name = fields.get(key).and_then(Value::as_str);
            choices.iter().copied().find(|option| Some(*option) == name).unwrap_or(fallback)
        };
        let flag = |key: &str, fallback| fields.get(key).and_then(Value::as_bool).unwrap_or(fallback);
        Self {
            glow: fields.get("glow").and_then(Value::as_str).and_then(Palette::from_name)
                .map_or(defaults.glow, Palette::name),
            shape: choice("shape", &["soft", "sharp", "round"], defaults.shape),
            size: choice("size", &["small", "medium", "full"], defaults.size),
            strength: fields.get("strength").and_then(Value::as_f64)
                .filter(|v| (0.4..=1.6).contains(v)).unwrap_or(defaults.strength),
            brows: fields.get("brows").and_then(Value::as_bool).unwrap_or_default(),
            mouth: fields.get("mouth").and_then(Value::as_bool).unwrap_or_default(),
            effects: flag("effects", defaults.effects),
            eye_details: flag("eye_details", defaults.eye_details),
            mood_colour: flag("mood_colour", defaults.mood_colour),
            clock_widget: fields.get("clock_widget").and_then(Value::as_bool).unwrap_or_default(),
            system_widget: fields.get("system_widget").and_then(Value::as_bool).unwrap_or_default(),
            weather_widget: fields.get("weather_widget").and_then(Value::as_bool).unwrap_or_default(),
            menu_position: choice("menu_position", &["left", "center", "right", "off"], defaults.menu_position),
            menu_labels: flag("menu_labels", defaults.menu_labels),
            clock_widget_position: choice("clock_widget_position", &CORNERS, defaults.clock_widget_position),
            system_widget_position: choice("system_widget_position", &CORNERS, defaults.system_widget_position),
            weather_widget_position: choice("weather_widget_position", &CORNERS, defaults.weather_widget_position),
            captions: flag("captions", defaults.captions),
            caption_cost: flag("caption_cost", defaults.caption_cost),
            caption_heard: flag("caption_heard", defaults.caption_heard),
            idle_glances: flag("idle_glances", defaults.idle_glances),
            revision: fields.get("revision").and_then(Value::as_u64)
                .filter(|v| *v < (1 << 53)).unwrap_or(0),
        }
    }

    pub fn read(path: &Path) -> Option<Self> {
        let mut contents = Vec::new();
        std::fs::File::open(path).ok()?.take((MAX_BYTES + 1) as u64).read_to_end(&mut contents).ok()?;
        if contents.len() > MAX_BYTES {
            return None;
        }
        let fields = serde_json::from_slice(&contents).ok()?;
        Some(Self::decode(&fields))
    }

    pub fn requested(event: &Event) -> Self {
        Self::decode(&event.data)
    }

    pub fn applied(self) -> Event {
        Event::new("screen.prefs_state")
            .with("glow", self.glow).with("shape", self.shape).with("strength", self.strength)
            .with("size", self.size).with("brows", self.brows).with("mouth", self.mouth).with("revision", self.revision)
            .with("effects", self.effects).with("eye_details", self.eye_details).with("mood_colour", self.mood_colour)
            .with("clock_widget", self.clock_widget).with("system_widget", self.system_widget)
            .with("weather_widget", self.weather_widget)
            .with("menu_position", self.menu_position).with("menu_labels", self.menu_labels)
            .with("clock_widget_position", self.clock_widget_position)
            .with("system_widget_position", self.system_widget_position)
            .with("weather_widget_position", self.weather_widget_position)
            .with("captions", self.captions).with("caption_cost", self.caption_cost).with("caption_heard", self.caption_heard)
            .with("idle_glances", self.idle_glances)
    }

    pub fn widget_positions(self) -> [Corner; 3] {
        [self.clock_widget_position, self.system_widget_position, self.weather_widget_position].map(Corner::from_name)
    }

    pub fn palette(self, look: &mut render::Look) {
        Palette::from_name(self.glow).unwrap_or_default().apply(look);
        look.glow_r = render::Look::default().glow_r * self.strength as f32;
    }

    pub fn eyes(self, mut uniforms: [f32; 32]) -> [f32; 32] {
        let strength = (self.strength as f32).sqrt();
        for offset in [0, 16] {
            uniforms[offset + 4] = match self.shape {
                "sharp" => (uniforms[offset + 4] + 1.4).max(2.0),
                "round" => 2.0,
                _ => uniforms[offset + 4],
            };
            let glow = uniforms[offset + 12];
            uniforms[offset + 12] = (glow * strength).clamp(glow.min(1.0), glow.max(1.2));
        }
        uniforms
    }

    pub fn eye_rect(self, mut rect: panel::Rect, w: i32, h: i32, amount: f32) -> panel::Rect {
        let scale = match self.size {
            "small" => 0.72,
            "medium" => 0.86,
            _ => 1.0,
        };
        // Interpolate the inset to zero while docking, keeping the ordinary
        // top-centre strip independent of the owner's empty-screen size.
        let inset = (1.0 - scale) * (1.0 - panel::smoothstep(amount)) / 2.0;
        let dx = ((w as f32 * inset).round() as i32).min((rect.w - 1).max(0) / 2);
        let dy = ((h as f32 * inset).round() as i32).min((rect.h - 1).max(0) / 2);
        rect.x += dx;
        rect.y += dy;
        rect.w -= 2 * dx;
        rect.h -= 2 * dy;
        rect
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn prefs(value: Value) -> Prefs {
        Prefs::decode(value.as_object().unwrap())
    }

    #[test]
    fn palette_stays_visible_to_the_current_lab_rule() {
        for glow in ["cyan", "blue"] {
            let mut look = render::Look::default();
            prefs(json!({"glow": glow})).palette(&mut look);
            for [red, green, blue] in [look.fill, look.core, look.halo] {
                assert!(255.0 * (blue - red) > 30.0 && green > red);
            }
        }
    }

    #[test]
    fn fields_default_independently_and_state_echoes_the_validated_look() {
        let look = prefs(json!({"glow": "sepia", "shape": "sharp", "strength": true, "size": "small",
                                "brows": true, "mouth": "on", "revision": 4, "clock_widget": true, "system_widget": "on"}));
        assert_eq!(look, Prefs { glow: "cyan", shape: "sharp", strength: 1.0, size: "small",
                                 brows: true, mouth: false, revision: 4, clock_widget: true, system_widget: false,
                                 ..Prefs::default() });
        assert_eq!(Prefs::requested(&look.applied()), look);
    }

    #[test]
    fn every_authored_colour_round_trips_through_preferences() {
        for name in Palette::NAMES {
            let prefs = prefs(json!({"glow": name}));
            assert_eq!(prefs.glow, *name);
            assert_eq!(Prefs::requested(&prefs.applied()), prefs);
        }
    }

    #[test]
    fn the_new_face_layers_are_independently_opt_in() {
        let old = prefs(json!({"brows": true, "mouth": true}));
        assert!(!old.effects && !old.eye_details && !old.mood_colour);
        let enabled = prefs(json!({"effects": true, "eye_details": "on", "mood_colour": true}));
        assert!(enabled.effects && !enabled.eye_details && enabled.mood_colour);
        assert_eq!(Prefs::requested(&enabled.applied()), enabled);
    }

    #[test]
    fn a_restarted_face_reads_the_durable_look_before_the_stale_mirror() {
        let dir = std::env::temp_dir().join(format!("face-look-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let state = dir.join("mind.json");
        let mirror = dir.join("runtime.json");
        std::fs::write(&state, r#"{"glow":"blue","revision":4}"#).unwrap();
        std::fs::write(&mirror, r#"{"glow":"cyan","revision":2}"#).unwrap();
        let first_frame = Prefs::startup(&state, &mirror);
        assert_eq!(first_frame.glow, "blue");
        assert!(first_frame.revision > Prefs::read(&mirror).unwrap().revision);
        std::fs::remove_file(&state).unwrap();
        assert_eq!(Prefs::startup(&state, &mirror), Prefs::read(&mirror).unwrap());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn strength_and_shape_preserve_preset_bounds_and_the_dock_ignores_size() {
        let p = prefs(json!({"strength": 1.6, "shape": "sharp", "size": "small"}));
        let mut eye = [0.0; 32];
        eye[4] = 2.3;
        eye[12] = 1.0;
        eye[28] = 0.3;
        assert_eq!(Prefs::default().eyes(eye), eye);
        let changed = p.eyes(eye);
        assert!((changed[4] - 3.7).abs() < 0.001);
        assert!((changed[12] - 1.2).abs() < 0.001);
        assert!(changed[28] < 0.4);
        let (w, h) = (1280, 800);
        assert!(p.eye_rect(panel::layout(w, h, 0.0).eyes, w, h, 0.0).h < h);
        let dock = panel::layout(w, h, 1.0).eyes;
        assert_eq!(p.eye_rect(dock, w, h, 1.0), dock);
    }
}

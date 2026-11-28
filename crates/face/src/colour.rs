//! Eye palettes from Shell Lab and the independently switchable Face Lab mood colour.

use crate::{eye::MoodTint, render::Look};

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Palette {
    name: &'static str,
    colours: [[f32; 3]; 3],
}

impl Palette {
    const fn new(name: &'static str, fill: [f32; 3], core: [f32; 3], halo: [f32; 3]) -> Self {
        Self {
            name,
            colours: [fill, core, halo],
        }
    }

    pub const NAMES: &'static [&'static str] = &{
        let mut names = [""; PALETTES.len()];
        let mut index = 0;
        while index < PALETTES.len() {
            names[index] = PALETTES[index].name;
            index += 1;
        }
        names
    };

    pub fn from_name(name: &str) -> Option<Self> {
        let name = name.trim();
        if let Some(palette) = PALETTES
            .iter()
            .copied()
            .find(|palette| palette.name.eq_ignore_ascii_case(name))
        {
            return Some(palette);
        }
        let name = name.to_ascii_lowercase();
        let canonical = match name.as_str() {
            "default" => "cyan",
            "aqua" | "turquoise" => "teal",
            "purple" | "indigo" => "violet",
            "lime" => "green",
            "orange" => "amber",
            "pink" | "magenta" => "rose",
            "gold" => "yellow",
            "silver" | "grey" | "gray" => "white",
            _ => return None,
        };
        PALETTES
            .iter()
            .copied()
            .find(|palette| palette.name == canonical)
    }

    pub fn name(self) -> &'static str {
        self.name
    }

    pub fn apply(self, look: &mut Look) {
        [look.fill, look.core, look.halo] = self.colours;
    }
}

impl Default for Palette {
    fn default() -> Self {
        PALETTES[0]
    }
}

// Cyan is the factory eye look. The next four palettes match the Shell Lab
// swatches. Shell Lab names the remaining six choices without RGB values.
#[rustfmt::skip]
const PALETTES: [Palette; 11] = [
    Palette::new("cyan",   [0.03, 0.86, 1.00], [0.30, 0.98, 1.00], [0.00, 0.48, 1.00]),
    Palette::new("blue",   [0.10, 0.45, 1.00], [0.42, 0.70, 1.00], [0.05, 0.25, 1.00]),
    Palette::new("teal",   [0.00, 0.80, 0.80], [0.25, 0.95, 0.92], [0.00, 0.55, 0.70]),
    Palette::new("mint",   [0.10, 0.95, 0.75], [0.35, 1.00, 0.85], [0.00, 0.70, 0.60]),
    Palette::new("violet", [0.35, 0.40, 1.00], [0.55, 0.62, 1.00], [0.25, 0.28, 1.00]),
    Palette::new("green",  [0.14, 0.90, 0.26], [0.45, 1.00, 0.53], [0.04, 0.57, 0.10]),
    Palette::new("amber",  [1.00, 0.48, 0.06], [1.00, 0.70, 0.36], [0.76, 0.24, 0.03]),
    Palette::new("red",    [1.00, 0.17, 0.11], [1.00, 0.39, 0.33], [0.74, 0.06, 0.07]),
    Palette::new("rose",   [1.00, 0.30, 0.59], [1.00, 0.59, 0.77], [0.71, 0.10, 0.35]),
    Palette::new("yellow", [1.00, 0.83, 0.16], [1.00, 0.97, 0.48], [0.72, 0.51, 0.05]),
    Palette::new("white",  [0.89, 0.92, 0.97], [1.00, 1.00, 1.00], [0.57, 0.65, 0.78]),
];

/// The Face Lab tint transition uses four parallel springs. Their exact
/// solution keeps captures independent of how often the face is drawn.
#[derive(Clone, Copy)]
pub struct Mood {
    value: [f32; 4],
    velocity: [f32; 4],
}

impl Default for Mood {
    fn default() -> Self {
        Self::still(crate::eye::ExprId::Neutral.visual().tint, 1.0)
    }
}

impl Mood {
    pub fn still(tint: MoodTint, intensity: f32) -> Self {
        Self {
            value: target(tint, intensity),
            velocity: [0.0; 4],
        }
    }

    pub fn update(&mut self, tint: MoodTint, intensity: f32, seconds: f32) {
        if seconds <= 0.0 {
            return;
        }
        // Roots of x'' + 15x' + 55x = 0, the Face Lab colour spring.
        const SLOW: f32 = -6.381_966;
        const FAST: f32 = -8.618_034;
        let slow = (SLOW * seconds).exp();
        let fast = (FAST * seconds).exp();
        for ((value, velocity), target) in self
            .value
            .iter_mut()
            .zip(&mut self.velocity)
            .zip(target(tint, intensity))
        {
            let offset = *value - target;
            let first = (*velocity - FAST * offset) / (SLOW - FAST);
            let second = offset - first;
            *value = target + first * slow + second * fast;
            *velocity = SLOW * first * slow + FAST * second * fast;
            if (*value - target).abs() < 0.0001 && velocity.abs() < 0.0001 {
                *value = target;
                *velocity = 0.0;
            }
        }
    }

    /// Per-expression hues use the same screen-space mixing as Face Lab.
    pub fn apply(self, look: &mut Look) {
        let [amount, red, green, blue] = self.value;
        let amount = amount.clamp(0.0, 1.0);
        let accent = [red, green, blue].map(|channel| channel.clamp(0.0, 1.0));
        let fill = mix([44.0 / 255.0, 237.0 / 255.0, 1.0], accent, amount);
        look.fill = fill;
        look.core = mix(fill, [1.0; 3], 0.5 * (1.0 - 0.7 * amount));
        look.halo = mix([0.0, 176.0 / 255.0, 1.0], fill, 0.5);
    }
}

fn mix(from: [f32; 3], to: [f32; 3], amount: f32) -> [f32; 3] {
    std::array::from_fn(|i| from[i] + (to[i] - from[i]) * amount)
}

fn target(tint: MoodTint, intensity: f32) -> [f32; 4] {
    let [red, green, blue] = tint.colour;
    [tint.amount * intensity.clamp(0.0, 1.5), red, green, blue]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::eye::ExprId;

    #[test]
    fn shell_palette_names_resolve_to_distinct_looks_with_aliases() {
        let default = Look::default();
        let mut applied = default;
        Palette::default().apply(&mut applied);
        assert_eq!(
            [applied.fill, applied.core, applied.halo],
            [default.fill, default.core, default.halo]
        );
        for (index, name) in Palette::NAMES.iter().enumerate() {
            let palette = Palette::from_name(name).unwrap();
            assert_eq!(palette.name(), *name);
            assert!(PALETTES[..index]
                .iter()
                .all(|other| other.colours != palette.colours));
        }
        assert_eq!(Palette::from_name("Pink"), Palette::from_name("rose"));
        assert_eq!(Palette::from_name("default"), Some(Palette::default()));
        assert!(Palette::from_name("sepia").is_none());
    }

    #[test]
    fn authored_tint_and_colour_spring_match_across_frame_rates() {
        let tint = ExprId::Love.visual().tint;
        assert_eq!(
            tint,
            MoodTint {
                amount: 1.0,
                colour: [1.0, 0.28, 0.58]
            }
        );
        let mut still = Look::default();
        Mood::still(tint, 1.0).apply(&mut still);
        for (actual, expected) in still.fill.into_iter().zip(tint.colour) {
            assert!((actual - expected).abs() < 1e-6);
        }
        let transition = |fps| {
            let mut mood = Mood::default();
            for _ in 0..fps {
                mood.update(tint, 1.0, 1.0 / fps as f32);
            }
            mood.value
        };
        for (left, right) in transition(20).into_iter().zip(transition(60)) {
            assert!((left - right).abs() < 1e-5);
        }
    }
}

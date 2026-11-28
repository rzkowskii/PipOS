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
    // A pale azure core, not white: the centre and its 1.35 capture glint
    // must stay off-white after the shader's roll-off (never white).
    Palette::new("white",  [0.89, 0.92, 0.97], [0.80, 0.88, 1.00], [0.57, 0.65, 0.78]),
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

    /// Per-expression hues use the same screen-space mixing as Face Lab, but
    /// mixed onto the owner's palette already in `look`: at amount 0 the eyes
    /// keep that palette exactly, and at amount 1 they match Face Lab's fill.
    pub fn apply(self, look: &mut Look) {
        let [amount, red, green, blue] = self.value;
        let amount = amount.clamp(0.0, 1.0);
        let accent = [red, green, blue].map(|channel| channel.clamp(0.0, 1.0));
        // Face Lab's core at full tint is 15% toward white; never more, so a
        // pale accent's centre stays off-white even under the capture glint.
        let hot = mix(accent, [1.0; 3], 0.15);
        look.fill = mix(look.fill, accent, amount);
        look.core = off_white(mix(look.core, hot, amount));
        look.halo = mix(look.halo, accent, 0.5 * amount);
    }
}

/// Two pale colours (the white palette and a gold tint, say) can mix to a
/// grey-white. Holding the dimmest channel at or under 0.82 keeps the core,
/// and its 1.35 capture glint, off white without touching any palette core.
fn off_white(mut core: [f32; 3]) -> [f32; 3] {
    let dimmest = (0..3).min_by(|&a, &b| core[a].total_cmp(&core[b])).unwrap_or(0);
    core[dimmest] = core[dimmest].min(0.82);
    core
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

    /// eye.frag's screen encoding: gamma 2, then the soft roll-off above 0.92.
    fn encode(screen: f32, linear_gain: f32) -> f32 {
        let v = (screen * screen * linear_gain).sqrt();
        v.min(0.92) + 0.08 * (1.0 - (-(v - 0.92).max(0.0) / 0.08).exp())
    }

    /// The core and its 1.35 capture glint never encode to white: the
    /// dimmest channel stays at least a dozen 8-bit levels under the 254
    /// that a pure-white core reaches after the roll-off.
    fn assert_never_white(core: [f32; 3], what: &str) {
        for gain in [0.35, 0.6, 1.0, 1.35] {
            let dimmest = core.map(|c| encode(c, gain)).into_iter().fold(1.0, f32::min);
            assert!(dimmest <= 0.95, "{what} core {core:?} at gain {gain} encodes to {dimmest}");
        }
    }

    #[test]
    fn no_palette_core_encodes_to_white() {
        for palette in PALETTES {
            assert_never_white(palette.colours[1], palette.name);
        }
    }

    #[test]
    fn neutral_mood_keeps_the_chosen_palette_and_no_tint_whitens_the_core() {
        for palette in PALETTES {
            let mut chosen = Look::default();
            palette.apply(&mut chosen);
            let mut tinted = chosen;
            Mood::default().apply(&mut tinted);
            assert_eq!([tinted.fill, tinted.core, tinted.halo], [chosen.fill, chosen.core, chosen.halo],
                       "{}", palette.name);
            for &id in ExprId::ALL {
                for intensity in [0.25, 0.5, 1.0, 1.5] {
                    let mut look = chosen;
                    Mood::still(id.visual().tint, intensity).apply(&mut look);
                    assert_never_white(look.core, &format!("{} {}", palette.name, id.name()));
                }
            }
        }
    }
}

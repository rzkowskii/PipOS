//! Short, repeatable motions for the expression catalogue's visual effects.
//! Each effect samples a bounded set of beats. A skipped frame never queues
//! particles, and rendering the same seed and time gives the same result.

use std::f32::consts::{PI, TAU};

const CAPACITY: usize = 96;
const STRIDE: usize = 8;
const SIDE: f32 = 1.38;
const TOP: f32 = 0.85;

pub const CHANNELS: usize = 26;
pub const NAMES: [&str; CHANNELS] = [
    "sweat", "cry", "blush", "hearts", "sparkle", "zzz", "question", "exclaim", "anger", "steam",
    "notes", "bulb", "dots", "gloom", "shiver", "bounce", "sway", "breath", "snow", "flame",
    "glitch", "scan", "bubble", "check", "warn", "battery",
];
pub const QUESTION: usize = 6;
pub const DOTS: usize = 12;
pub const SHIVER: usize = 14;
pub const BOUNCE: usize = 15;
pub const SWAY: usize = 16;
pub const BREATH: usize = 17;
pub const GLITCH: usize = 20;
pub const BATTERY: usize = 25;

pub fn channel(name: &str) -> Option<usize> {
    let name = match name {
        "tear" | "tears" => "cry",
        "sleep" => "zzz",
        "low-battery" => "battery",
        "warning" => "warn",
        name => name,
    };
    NAMES.iter().position(|channel| *channel == name)
}

// Keep the vertex uniform capacity in step with CAPACITY * 2 vec4 values.
pub const VERT: &str = r#"
uniform vec2 uRes;
uniform vec4 uSprites[192];
out vec2 vLocal;
out float vScreenX;
flat out vec4 vSprite; // shape, opacity, variant, edge ramp

void main() {
    int id = gl_InstanceID * 2;
    vec4 rect = uSprites[id];
    vec4 style = uSprites[id + 1];
    int i = gl_VertexID;
    vec2 q = vec2(i == 1 || i == 4 || i == 5 ? 1.0 : -1.0,
                  i == 2 || i == 3 || i == 5 ? 1.0 : -1.0);
    float c = cos(style.z), s = sin(style.z);
    vec2 box = q * rect.zw;
    vec2 p = rect.xy + vec2(c * box.x - s * box.y, s * box.x + c * box.y);
    gl_Position = vec4(p * vec2(uRes.y / uRes.x, 1.0), 0.0, 1.0);
    vLocal = q;
    vScreenX = p.x;
    vSprite = vec4(style.x, style.y, style.w, 2.0 / (uRes.y * min(rect.z, rect.w)));
}
"#;

pub const FRAG: &str = include_str!("emote.frag");

#[derive(Clone, Copy)]
pub struct Sprites {
    values: [f32; CAPACITY * STRIDE],
    count: usize,
}

impl Default for Sprites {
    fn default() -> Self {
        Self {
            values: [0.0; CAPACITY * STRIDE],
            count: 0,
        }
    }
}

/// Corresponds to the shape dispatch in emote.frag.
#[repr(u8)]
#[derive(Clone, Copy)]
enum Shape {
    Drop,
    Line,
    Heart,
    Spark,
    Z,
    Question,
    Exclaim,
    Anger,
    Puff,
    Note,
    Bulb,
    Dot,
    Snow,
    Flame,
    Scan,
    Bubble,
    Check,
    Warning,
    Battery,
    TearTrail,
    Blush,
}

impl Sprites {
    pub fn len(&self) -> i32 {
        self.count as i32
    }

    pub fn uniforms(&self) -> &[f32] {
        &self.values[..self.count * STRIDE]
    }

    fn push(
        &mut self,
        shape: Shape,
        center: [f32; 2],
        size: [f32; 2],
        angle: f32,
        alpha: f32,
        variant: f32,
    ) {
        if self.count == CAPACITY || !alpha.is_finite() || alpha <= 0.012 {
            return;
        }
        let [sx, sy] = size;
        let (sine, cosine) = angle.sin_cos();
        let rx = (cosine.abs() * sx + sine.abs() * sy).min(SIDE);
        let ry = (sine.abs() * sx + cosine.abs() * sy).min(TOP);
        let x = center[0].clamp(-SIDE + rx, SIDE - rx);
        let y = center[1].clamp(-TOP + ry, TOP - ry);
        let offset = self.count * STRIDE;
        self.values[offset..offset + STRIDE].copy_from_slice(&[
            x,
            y,
            sx,
            sy,
            shape as u8 as f32,
            alpha.min(1.0),
            angle,
            variant,
        ]);
        self.count += 1;
    }
}

/// Two time scales are useful: a repeated phase for particles and a smooth
/// oscillation for glyphs. Keep the reduction in f64 so long uptimes retain
/// the same resolution as the preview.
fn phase(seconds: f64, rate: f64, offset: f32) -> f32 {
    (seconds * rate + f64::from(offset)).rem_euclid(1.0) as f32
}

fn beat(seconds: f64, rate: f64, offset: f32) -> f32 {
    (phase(seconds, rate, offset) * TAU).sin()
}

/// The torn bands drift sideways without altering the eyes' luminance.
pub fn glitch(amount: f32, seconds: f64) -> [f32; 4] {
    if !amount.is_finite() || amount <= 0.0 || !seconds.is_finite() {
        return [0.0; 4];
    }
    let strength = amount.clamp(0.0, 1.0);
    [
        strength * 0.095 * beat(seconds, 0.59, 0.14),
        strength * 0.086 * beat(seconds, 0.47, 0.66),
        strength * 0.072 * beat(seconds, 0.38, 0.36),
        strength,
    ]
}

fn window(phase: f32, attack: f32, release: f32) -> f32 {
    (phase / attack).clamp(0.0, 1.0) * ((1.0 - phase) / release).clamp(0.0, 1.0)
}

fn salt(seed: u32, slot: u32) -> f32 {
    let mut n = seed.wrapping_add(slot.wrapping_mul(0x9e37_79b9));
    n = (n ^ (n >> 16)).wrapping_mul(0x21f0_aaad);
    n = (n ^ (n >> 15)).wrapping_mul(0x735a_2d97);
    ((n ^ (n >> 15)) >> 8) as f32 / 16_777_216.0
}

pub fn sprites(
    effects: &[f32; CHANNELS],
    burst: f32,
    eyes: &[f32; 32],
    seconds: f64,
    seed: u32,
) -> Sprites {
    // Body motion and glitch affect the eye shader instead of adding sprites.
    let [sweat, cry, blush, hearts, sparkle, zzz, question, exclaim, anger, steam, notes, bulb, dots, gloom, _shiver, _bounce, _sway, _breath, snow, flame, _glitch, scan, bubble, check, warn, battery] =
        *effects;
    let mut out = Sprites::default();
    if !(burst > 0.012
        || effects
            .iter()
            .enumerate()
            .any(|(index, amount)| !matches!(index, SHIVER..=BREATH | GLITCH) && *amount > 0.012))
    {
        return out;
    }

    // Symbols tied to an eye move with the animated pose.
    for (index, side) in [-1.0_f32, 1.0].into_iter().enumerate() {
        let offset = index * 16;
        let [x, y, w, h] = [
            eyes[offset],
            eyes[offset + 1],
            eyes[offset + 2],
            eyes[offset + 3],
        ];
        let side_index = index as f32;
        if sweat > 0.012 {
            let variation = salt(seed, 70 + index as u32);
            let p = phase(seconds, 0.48, 0.32 + side_index * 0.51);
            let fall = ((p - 0.24).max(0.0) / 0.76).powi(2);
            let fly = 0.02 + 0.09 * sweat.min(1.0);
            let size = (0.045 + 0.022 * variation) * (p / 0.24).clamp(0.33, 1.0);
            let side_alpha = if side < 0.0 { 0.72 } else { 1.0 };
            out.push(
                Shape::Drop,
                [
                    side * (1.28 + fly * fall),
                    0.45 + 0.13 * variation - fall * 0.91,
                ],
                [size, 1.51 * size],
                -side * 0.22,
                sweat * side_alpha * window(p, 0.24, 0.28),
                0.95,
            );
        }
        if cry > 0.012 {
            for lane in 0..2 {
                let p = phase(seconds, 0.65, lane as f32 * 0.50 + side_index * 0.24);
                let variation = salt(seed, 90 + 2 * index as u32 + lane);
                let start = y - h * (0.57 + 0.24 * variation);
                let x = x + (variation - 0.5) * w;
                let fall = ((p - 0.23).max(0.0) / 0.77).powi(2) * 0.91;
                let below = start - fall;
                let alpha = cry * window(p, 0.18, 0.33) * ((below + 0.85) / 0.14).clamp(0.0, 1.0);
                if fall > 0.045 {
                    out.push(
                        Shape::TearTrail,
                        [x, (start + below) * 0.5],
                        [0.022, fall * 0.5],
                        0.0,
                        alpha * 0.69,
                        0.0,
                    );
                }
                out.push(
                    Shape::Drop,
                    [x, below],
                    [0.038 + 0.025 * p, 0.062 + 0.050 * p],
                    0.0,
                    alpha,
                    1.0,
                );
            }
        }
        if blush > 0.012 {
            out.push(
                Shape::Blush,
                [side * 0.98, -0.36],
                [0.25, 0.13],
                0.0,
                blush * 0.14,
                0.0,
            );
            for hatch in -1..=1 {
                out.push(
                    Shape::Line,
                    [side * 0.98 + hatch as f32 * 0.085, -0.36],
                    [0.019, 0.072],
                    -0.57,
                    blush * 0.95,
                    0.0,
                );
            }
        }
        if flame > 0.012 {
            for tongue in -1..=1 {
                let sway = beat(seconds, 1.75, tongue as f32 / 3.0 + side_index * 0.27);
                let height =
                    (0.2 + 0.09 * sway) * flame.min(1.0) * if tongue == 0 { 1.5 } else { 1.0 };
                out.push(
                    Shape::Flame,
                    [
                        x + tongue as f32 * 0.17,
                        (y + h + 0.23).max(0.49) + height * 0.33,
                    ],
                    [0.091, (height * 0.55).max(0.018)],
                    0.0,
                    flame * 0.95,
                    beat(seconds, 1.43, tongue as f32 / 5.0 + side_index * 0.23),
                );
            }
        }
        if burst > 0.012 {
            for ray in 0..9 {
                let angle = ray as f32 * TAU / 9.0 + 0.2 + side_index * 0.3;
                let growth = (seconds / 0.12).clamp(0.0, 1.0) as f32;
                let start = w.max(h) * 1.30 + 0.05 * growth;
                let length = (0.07 + 0.10 * growth) * if ray % 2 == 0 { 1.35 } else { 1.0 };
                let radius = start + length * 0.5;
                out.push(
                    Shape::Line,
                    [x + angle.cos() * radius, y + angle.sin() * radius],
                    [0.024, length * 0.88],
                    angle - PI * 0.5,
                    burst * 0.80,
                    0.5,
                );
            }
        }
    }

    if hearts > 0.012 {
        for slot in 0..4 {
            let jitter = salt(seed, 10 + slot);
            let p = phase(seconds, 0.39, 0.19 + slot as f32 * 0.25);
            let x = [-0.93, 1.03, 0.12, -0.19][slot as usize]
                + 0.13 * (jitter - 0.5)
                + 0.055 * beat(seconds, 0.38, jitter);
            let r = 0.098 + 0.060 * jitter;
            out.push(
                Shape::Heart,
                [x, -0.48 + 1.05 * p],
                [r, r],
                (jitter - 0.5) * 0.55,
                hearts * window(p, 0.17, 0.31),
                0.0,
            );
        }
    }
    if sparkle > 0.012 {
        for slot in 0..6 {
            let p = phase(seconds, 0.81, salt(seed, 20 + slot));
            let pulse = (p * PI).sin().max(0.0);
            let [x, y] = [
                [-1.23, 0.59],
                [1.23, 0.57],
                [-1.22, -0.52],
                [1.22, -0.50],
                [0.0, 0.68],
                [0.34, -0.55],
            ][slot as usize];
            let r = (0.059 + salt(seed, 30 + slot) * 0.042) * (0.61 + 0.39 * pulse);
            out.push(
                Shape::Spark,
                [x, y],
                [r, r],
                slot as f32 * 0.23,
                sparkle * pulse,
                0.0,
            );
        }
    }
    if zzz > 0.012 {
        for slot in 0..2 {
            let p = phase(seconds, 0.40, 0.31 + slot as f32 * 0.49);
            let r = 0.058 + p * 0.037;
            out.push(
                Shape::Z,
                [0.97 + p * 0.27, 0.32 + p * 0.48],
                [r, r],
                0.16,
                zzz * window(p, 0.18, 0.30),
                0.0,
            );
        }
    }
    out.push(
        Shape::Question,
        [1.20, 0.69 + 0.017 * beat(seconds, 0.47, 0.0)],
        [0.114, 0.153],
        0.13 + 0.07 * beat(seconds, 0.32, 0.0),
        question,
        0.0,
    );
    let surprise_shake = if seconds < 0.6 {
        0.012 * (1.0 - seconds / 0.6) as f32 * beat(seconds, 6.2, 0.0)
    } else {
        0.0
    };
    out.push(
        Shape::Exclaim,
        [1.20 + surprise_shake, 0.67],
        [0.079, 0.177],
        -0.09,
        exclaim,
        0.0,
    );
    let vein = 0.139 * (1.0 + 0.12 * beat(seconds, 1.43, 0.0));
    out.push(Shape::Anger, [1.17, 0.69], [vein, vein], 0.0, anger, 0.0);

    if steam > 0.012 {
        for slot in 0..4 {
            let jitter = salt(seed, 80 + slot);
            let p = phase(seconds, 0.86, 0.22 + slot as f32 * 0.25);
            let side = if slot % 2 == 0 { -1.0 } else { 1.0 };
            let x = side * (1.19 + 0.07 * p + 0.05 * jitter);
            let r = 0.04 + p * 0.060;
            out.push(
                Shape::Puff,
                [x, 0.43 + p * (0.22 + jitter * 0.11)],
                [r, r],
                0.0,
                steam * 0.49 * window(p, 0.22, 0.52),
                0.0,
            );
        }
    }
    if notes > 0.012 {
        for slot in 0..2 {
            let p = phase(seconds, 0.47, 0.21 + slot as f32 * 0.5);
            let side = if slot == 0 { -1.0 } else { 1.0 };
            let x = side * 1.19 + 0.044 * beat(seconds, 0.54, slot as f32 * 0.5);
            out.push(
                Shape::Note,
                [x, -0.36 + p * 1.07],
                [0.102, 0.13],
                0.18 * side,
                notes * window(p, 0.23, 0.29),
                slot as f32,
            );
        }
    }
    out.push(
        Shape::Bulb,
        [0.0, 0.70 + 0.013 * beat(seconds, 0.47, 0.0)],
        [0.179, 0.154],
        0.0,
        bulb,
        0.60 + 0.40 * beat(seconds, 1.27, 0.0),
    );
    if dots > 0.012 {
        for dot in 0..3 {
            let p = 0.5 + 0.5 * beat(seconds, 0.65, -0.15 * dot as f32);
            let r = 0.041 + 0.009 * p;
            out.push(
                Shape::Dot,
                [1.04 + dot as f32 * 0.129, 0.67 + 0.028 * p],
                [r, r],
                0.0,
                dots * (0.36 + 0.62 * p),
                0.0,
            );
        }
    }
    if gloom > 0.012 {
        for slot in 0..9 {
            let jitter = salt(seed, 40 + slot);
            let height = 0.18 + 0.15 * jitter;
            out.push(
                Shape::Line,
                [
                    -1.23 + slot as f32 * 0.31 + (jitter - 0.5) * 0.10,
                    TOP - height,
                ],
                [0.018, height],
                0.045 * beat(seconds, 0.23, jitter),
                gloom * 0.50,
                1.0,
            );
        }
    }
    if snow > 0.012 {
        for slot in 0..13 {
            let jitter = salt(seed, 110 + slot);
            let p = phase(
                seconds,
                0.19 + 0.05 * f64::from(jitter),
                salt(seed, 130 + slot),
            );
            let r = 0.031 + 0.026 * jitter;
            let x = -1.24 + slot as f32 * 0.206 + 0.064 * beat(seconds, 0.24, jitter);
            out.push(
                Shape::Snow,
                [x, 0.81 - p * 1.62],
                [r, r],
                phase(seconds, 0.08, jitter) * TAU,
                snow * window(p, 0.16, 0.22),
                0.0,
            );
        }
    }
    if scan > 0.012 {
        let p = phase(seconds, 0.54, 0.17);
        out.push(
            Shape::Scan,
            [0.0, 0.80 - p * 1.60],
            [SIDE, 0.108],
            0.0,
            scan * 0.83 * window(p, 0.16, 0.16),
            0.0,
        );
    }
    if bubble > 0.012 {
        let r = 0.085 + 0.033 * beat(seconds, 0.27, 0.18);
        out.push(
            Shape::Bubble,
            [0.34, -0.50],
            [r, r],
            0.0,
            bubble * 0.85,
            0.0,
        );
    }
    out.push(Shape::Check, [1.19, 0.67], [0.175, 0.151], 0.0, check, 0.0);
    out.push(
        Shape::Warning,
        [1.19, 0.67],
        [0.169, 0.167],
        0.0,
        warn * (0.55 + 0.45 * beat(seconds, 1.11, 0.0)),
        0.0,
    );
    out.push(
        Shape::Battery,
        [1.15, 0.73],
        [0.220, 0.102],
        0.0,
        battery,
        0.4 + 0.6 * beat(seconds, 0.80, 0.0).max(0.0),
    );
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_glyph_effect_emits_finite_sprites() {
        let eyes = crate::eye::ExprId::Neutral.pose().uniforms();
        for (index, name) in NAMES.into_iter().enumerate() {
            assert_eq!(channel(name), Some(index));
            if matches!(index, SHIVER..=BREATH | GLITCH) {
                continue;
            }
            let mut effects = [0.0; CHANNELS];
            effects[index] = 1.0;
            let mut appeared = false;
            for frame in 0..36 {
                let output = sprites(&effects, 0.0, &eyes, f64::from(frame) / 12.0, 42);
                appeared |= output.len() > 0;
                assert!(
                    output.uniforms().iter().all(|value| value.is_finite()),
                    "{name}"
                );
            }
            assert!(appeared, "{name}");
        }
    }

    #[test]
    fn a_full_effect_stack_preserves_the_screen_margin() {
        let eyes = crate::eye::ExprId::Surprise.pose().uniforms();
        for frame in 0..48 {
            let output = sprites(&[1.0; CHANNELS], 1.0, &eyes, f64::from(frame) / 12.0, 42);
            assert!(output.count < CAPACITY);
            for sprite in output.uniforms().chunks_exact(STRIDE) {
                let [x, y, w, h, _, _, tilt, _] = *sprite else {
                    unreachable!()
                };
                let (s, c) = tilt.sin_cos();
                assert!(x.abs() + c.abs() * w + s.abs() * h <= SIDE + 0.0001);
                assert!(y.abs() + s.abs() * w + c.abs() * h <= TOP + 0.0001);
            }
        }
    }

    #[test]
    fn torn_bands_move_continuously_within_the_offset_limit() {
        assert_eq!(glitch(0.0, 2.0), [0.0; 4]);
        let mut previous = glitch(1.0, 0.0);
        for frame in 1..240 {
            let next = glitch(1.0, f64::from(frame) / 60.0);
            assert_eq!(next[3], 1.0);
            for band in 0..3 {
                assert!(next[band].abs() <= 0.095);
                assert!((next[band] - previous[band]).abs() <= 0.006);
            }
            previous = next;
        }
    }
}

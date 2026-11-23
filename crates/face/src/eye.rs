//! Eye shape parameters and the expression presets.
//!
//! Units: the screen is 2 high, centred on the origin, y up. Lid heights are in
//! units of the eye's half-height: +1 is the top of the eye, -1 the bottom.

/// Numbers per eye.
pub const N: usize = 13;
/// Numbers per face.
pub const M: usize = 2 * N;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Eye {
    pub x: f32,
    pub y: f32,
    /// Half-width and half-height.
    pub w: f32,
    pub h: f32,
    /// Superellipse exponent: 2 is an ellipse, higher is squarer.
    pub round: f32,
    /// Rotation in radians, counter-clockwise.
    pub tilt: f32,
    /// Upper lid: height at the eye's centre line, slope, and bow (+ arches up).
    pub top: f32,
    pub top_slant: f32,
    pub top_curve: f32,
    /// Lower lid, same three numbers.
    pub bot: f32,
    pub bot_slant: f32,
    pub bot_curve: f32,
    /// Brightness.
    pub glow: f32,
}

/// Lid heights beyond these never touch the shape.
const OPEN_TOP: f32 = 1.5;
const OPEN_BOT: f32 = -1.5;

impl Eye {
    /// A plain open eye on the left of the screen.
    pub const OPEN: Eye = Eye {
        x: -0.80,
        y: 0.03,
        w: 0.46,
        h: 0.33,
        round: 2.3,
        tilt: 0.0,
        top: OPEN_TOP,
        top_slant: 0.0,
        top_curve: 0.0,
        bot: OPEN_BOT,
        bot_slant: 0.0,
        bot_curve: 0.0,
        glow: 1.0,
    };

    /// The same eye reflected to the other side of the face.
    pub fn mirror(self) -> Eye {
        Eye {
            x: -self.x,
            tilt: -self.tilt,
            top_slant: -self.top_slant,
            bot_slant: -self.bot_slant,
            ..self
        }
    }

    pub fn to_array(self) -> [f32; N] {
        [
            self.x,
            self.y,
            self.w,
            self.h,
            self.round,
            self.tilt,
            self.top,
            self.top_slant,
            self.top_curve,
            self.bot,
            self.bot_slant,
            self.bot_curve,
            self.glow,
        ]
    }

    pub fn from_slice(a: &[f32]) -> Eye {
        Eye {
            x: a[0],
            y: a[1],
            w: a[2],
            h: a[3],
            round: a[4],
            tilt: a[5],
            top: a[6],
            top_slant: a[7],
            top_curve: a[8],
            bot: a[9],
            bot_slant: a[10],
            bot_curve: a[11],
            glow: a[12],
        }
    }
}

/// Both eyes. `l` is the one on the left of the screen.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Pose {
    pub l: Eye,
    pub r: Eye,
}

impl Pose {
    /// A symmetric face from its left eye.
    pub fn sym(l: Eye) -> Pose {
        Pose { l, r: l.mirror() }
    }

    pub fn to_array(self) -> [f32; M] {
        let mut out = [0.0; M];
        out[..N].copy_from_slice(&self.l.to_array());
        out[N..].copy_from_slice(&self.r.to_array());
        out
    }

    pub fn from_array(a: &[f32; M]) -> Pose {
        Pose { l: Eye::from_slice(&a[..N]), r: Eye::from_slice(&a[N..]) }
    }

    /// Pack for the shader: four vec4 per eye.
    pub fn uniforms(self) -> [f32; 32] {
        let mut out = [0.0; 32];
        for (i, e) in [self.l, self.r].into_iter().enumerate() {
            let o = i * 16;
            out[o..o + 4].copy_from_slice(&[e.x, e.y, e.w, e.h]);
            out[o + 4..o + 8].copy_from_slice(&[e.round, e.tilt, e.top, e.top_slant]);
            out[o + 8..o + 12].copy_from_slice(&[e.top_curve, e.bot, e.bot_slant, e.bot_curve]);
            out[o + 12] = e.glow;
        }
        out
    }
}

/// Every expression the face knows, in display order.
pub const NAMES: &[&str] = &[
    "neutral",
    "attentive",
    "listening",
    "thinking",
    "speaking",
    "joy",
    "curiosity",
    "surprise",
    "skepticism",
    "concern",
    "sleepy",
    "asleep",
    "dreaming",
    "tired",
    "muted",
    "dazed",
];

/// Looks the face uses on its own that are not expressions the brainstem
/// can ask for (so they are not in NAMES, `--list` or the demo).
#[cfg_attr(not(test), allow(dead_code))]
pub const OWN: &[&str] = &["loading"];

/// The six from the original expression sheet, in its order.
pub const SHEET: &[&str] = &["joy", "curiosity", "surprise", "skepticism", "concern", "sleepy"];

pub fn preset(name: &str) -> Option<Pose> {
    let o = Eye::OPEN;
    let rest = rest();
    Some(match name {
        "neutral" | "speaking" => Pose::sym(rest),
        // The same egg, a touch larger, brighter and more upright: alert.
        "attentive" => Pose::sym(Eye { y: 0.05, w: 0.50, h: 0.365, tilt: -0.28, glow: 1.06, ..rest }),
        // A small head tilt, the way someone leans in to hear.
        "listening" => {
            let e = Eye { w: 0.50, h: 0.355, glow: 1.03, ..rest };
            Pose {
                l: Eye { y: 0.065, tilt: e.tilt - 0.07, ..e },
                r: Eye { y: -0.005, tilt: -e.tilt - 0.07, ..e.mirror() },
            }
        }
        // Eyes drift up and aside while the upper lids settle a little.
        "thinking" => Pose {
            l: Eye { x: -0.70, y: 0.10, top: 0.22, top_slant: -0.02, top_curve: 0.42, ..rest },
            r: Eye { x: 0.84, y: 0.10, top: 0.12, top_slant: 0.10, top_curve: 0.40, ..rest.mirror() },
        },
        "joy" => Pose::sym(Eye { x: -0.72, y: -0.18, w: 0.55, h: 0.47, round: 2.0, bot: 0.0, bot_curve: 0.50, glow: 1.05, ..o }),
        "curiosity" => Pose {
            l: Eye { x: -0.70, y: 0.03, w: 0.47, h: 0.43, round: 2.25, tilt: -0.10, glow: 1.04, ..o },
            r: Eye { x: 0.74, y: -0.12, w: 0.48, h: 0.28, round: 2.0, tilt: 0.40, top: 0.0, top_slant: -0.25, top_curve: 1.0, ..o },
        },
        "surprise" => Pose::sym(Eye { x: -0.68, y: 0.03, w: 0.45, h: 0.56, round: 2.05, glow: 1.1, ..o }),
        "skepticism" => Pose {
            l: Eye { x: -0.72, y: -0.12, w: 0.50, h: 0.075, round: 2.0, ..o },
            r: Eye { x: 0.74, y: 0.10, w: 0.54, h: 0.46, round: 2.1, top: 0.05, top_slant: 0.12, ..o },
        },
        "concern" => Pose::sym(Eye { x: -0.72, y: -0.12, w: 0.48, h: 0.36, round: 2.1, tilt: 0.26, top: 0.22, top_slant: 0.62, top_curve: 0.35, ..o }),
        "sleepy" => Pose::sym(Eye { x: -0.72, y: -0.12, w: 0.52, h: 0.22, top: 0.0, top_curve: -0.50, glow: 0.9, ..o }),
        "asleep" => Pose::sym(Eye { x: -0.72, y: -0.14, w: 0.50, h: 0.20, top: 0.0, top_curve: -0.82, glow: 0.30, ..o }),
        "dreaming" => Pose::sym(Eye { x: -0.72, y: -0.14, w: 0.50, h: 0.21, top: -0.04, top_curve: -0.76, glow: 0.34, ..o }),
        // Heavy lids over the resting egg, drooping toward the outside.
        "tired" => Pose::sym(Eye { y: -0.04, top: -0.02, top_slant: 0.22, top_curve: 0.18, glow: 0.55, ..rest }),
        "muted" => Pose::sym(Eye { x: -0.74, y: 0.0, w: 0.34, h: 0.05, round: 4.0, glow: 0.5, ..o }),
        // Waiting for the rest of the body at power-on: the resting egg,
        // calm and a little dimmer, its lids relaxed. Not sick, just early.
        "loading" => Pose::sym(Eye { h: 0.31, top: 0.26, glow: 0.78, ..rest }),
        "dazed" => Pose {
            l: Eye { x: -0.74, y: 0.08, w: 0.30, h: 0.30, round: 2.0, glow: 0.6, ..o },
            r: Eye { x: 0.78, y: -0.06, w: 0.40, h: 0.20, round: 2.0, tilt: -0.25, glow: 0.6, ..o },
        },
        _ => return None,
    })
}

/// The resting eye: a soft egg, broad and domed at its upper outer end and
/// narrowing to a smaller round at its lower inner end, so the pair leans
/// gently in toward each other (mockup-face). The ellipse is tipped inward;
/// in its own frame the two lids are near-tangent parabolas that pinch only
/// the inner half, which is what turns the ellipse into an egg. It sits where
/// Eye::OPEN does, so eyes closing from or opening to OPEN do not slide.
fn rest() -> Eye {
    Eye {
        x: -0.80,
        y: 0.03,
        w: 0.48,
        h: 0.34,
        round: 2.1,
        tilt: -0.34,
        top: 0.37,
        top_slant: -0.17,
        top_curve: 0.63,
        bot: -0.45,
        bot_slant: 0.10,
        bot_curve: -0.55,
        ..Eye::OPEN
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_name_has_a_preset() {
        for name in NAMES.iter().chain(SHEET).chain(OWN) {
            assert!(preset(name).is_some(), "missing preset: {name}");
        }
        assert!(preset("nonsense").is_none());
    }

    #[test]
    fn presets_stay_on_screen() {
        // A 16:10 screen spans x in [-1.6, 1.6] and y in [-1, 1].
        for name in NAMES.iter().chain(OWN) {
            let p = preset(name).unwrap();
            for e in [p.l, p.r] {
                assert!(e.x.abs() + e.w < 1.5, "{name} too wide");
                assert!(e.y.abs() + e.h < 0.9, "{name} too tall");
                assert!(e.w > 0.0 && e.h > 0.0 && e.round >= 2.0, "{name} degenerate");
            }
        }
    }

    #[test]
    fn mirror_twice_is_identity() {
        let e = Eye { tilt: 0.3, top_slant: 0.2, bot_slant: -0.4, ..Eye::OPEN };
        assert_eq!(e.mirror().mirror(), e);
        assert_eq!(e.mirror().x, -e.x);
    }

    #[test]
    fn array_round_trip() {
        let p = preset("curiosity").unwrap();
        assert_eq!(Pose::from_array(&p.to_array()), p);
    }
}

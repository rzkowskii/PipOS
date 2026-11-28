//! Optional face layers, shaped by the same expression that drives the eyes.

use crate::eye::ExprId;

#[derive(Clone, Copy, Debug, Default)]
pub struct Layers {
    pub brows: bool,
    pub mouth: bool,
    pub details: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct Motion {
    pub expression: ExprId,
    pub blend: Option<(ExprId, f32)>,
    pub strength: f32,
    pub viseme: crate::viseme::Sample,
    pub speech: f32,
    pub pupil_kick: f32,
    pub time: f64,
    pub reflected: bool,
}

#[derive(Clone, Copy)]
pub struct Decor {
    values: [f32; 40],
    moving: bool,
}

impl Default for Decor {
    fn default() -> Self {
        Self {
            values: [0.0; 40],
            moving: false,
        }
    }
}

pub fn allowed(expression: ExprId) -> bool {
    !matches!(
        expression,
        ExprId::Loading
            | ExprId::Muted
            | ExprId::Dazed
            | ExprId::Asleep
            | ExprId::Sleepy
            | ExprId::Dreaming
            | ExprId::Tired
    )
}

impl Decor {
    pub fn still(layers: Layers, expression: ExprId, time: f64) -> Self {
        let motion = Motion {
            expression,
            blend: None,
            strength: 1.0,
            viseme: crate::viseme::Sample::default(),
            speech: 0.0,
            pupil_kick: 0.0,
            time,
            reflected: false,
        };
        Self {
            values: Self::target(layers, motion),
            moving: false,
        }
    }

    pub fn update(&mut self, layers: Layers, motion: Option<Motion>, seconds: f32) {
        let Some(motion) = motion else {
            *self = Self::default();
            return;
        };
        if !layers.brows && !layers.mouth && !layers.details && !self.active() {
            return;
        }
        let target = Self::target(layers, motion);
        if !self.active() && !self.moving {
            // Put a newly visible feature in place before fading up from zero.
            for index in [0..6, 8..14, 16..29] {
                self.values[index.clone()].copy_from_slice(&target[index]);
            }
        }
        let step = 1.0 - (-18.0 * seconds.max(0.0)).exp();
        self.moving = false;
        for (index, (value, target)) in self.values.iter_mut().zip(target).enumerate() {
            if matches!(index, 30 | 39) || (*value - target).abs() < 0.0005 {
                *value = target;
            } else {
                *value += (target - *value) * step;
                self.moving = true;
            }
        }
    }

    fn target(layers: Layers, motion: Motion) -> [f32; 40] {
        let pack = |expression: ExprId| {
            let visual = expression.visual();
            let [left, right] = if motion.reflected {
                [visual.brows[1], visual.brows[0]]
            } else {
                visual.brows
            };
            let m = visual.mouth;
            let d = visual.details;
            let face = if motion.reflected { -1.0 } else { 1.0 };
            let mouth_phase = (motion.time * 9.0).rem_euclid(std::f64::consts::TAU) as f32;
            let eye_phase = (motion.time * 5.0).rem_euclid(std::f64::consts::TAU) as f32;
            [
                left.y,
                left.dx,
                left.len,
                left.tilt,
                left.curve,
                left.th,
                left.alpha,
                0.0,
                right.y,
                right.dx,
                right.len,
                right.tilt,
                right.curve,
                right.th,
                right.alpha,
                0.0,
                m.x * face,
                m.w,
                m.y,
                m.corner,
                m.skew * face,
                m.arc,
                (m.open + 0.72 * motion.speech + 0.18 * m.talk * mouth_phase.sin().abs())
                    .clamp(0.0, 1.1),
                m.round,
                m.cat,
                m.wave,
                m.zig,
                m.tongue,
                m.low,
                m.alpha,
                mouth_phase,
                0.0,
                d.heart,
                d.cross,
                d.spiral,
                d.star,
                d.pupil,
                d.shine,
                d.wet,
                eye_phase,
            ]
        };
        let mut values = pack(motion.expression);
        if let Some((other, weight)) = motion.blend {
            for (value, other) in values.iter_mut().zip(pack(other)) {
                *value += (other - *value) * weight;
            }
        }
        if motion.strength != 1.0 {
            for (value, neutral) in values.iter_mut().zip(pack(ExprId::Neutral)) {
                *value = neutral + (*value - neutral) * motion.strength;
            }
        }
        values[17] = (values[17] * (1.0 + motion.viseme.width)).max(0.025);
        values[22] = (values[22] + motion.viseme.open).clamp(0.0, 1.1);
        values[23] = (values[23] + motion.viseme.round).clamp(0.0, 1.0);
        for index in [6, 14, 24, 27, 29, 32, 33, 34, 35, 36, 37, 38] {
            values[index] = values[index].clamp(0.0, 1.0);
        }
        if !layers.brows {
            values[6] = 0.0;
            values[14] = 0.0;
        }
        if !layers.mouth {
            values[29] = 0.0;
        }
        if layers.details {
            values[36] = (values[36] - motion.pupil_kick).max(0.0);
        } else {
            values[32..39].fill(0.0);
        }
        values
    }

    pub fn active(&self) -> bool {
        self.values[6] > 0.0
            || self.values[14] > 0.0
            || self.values[29] > 0.0
            || self.values[32..39].iter().any(|value| *value > 0.0)
    }

    pub fn moving(&self) -> bool {
        self.active()
            && (self.moving
                || self.values[35] > 0.0
                || self.values[34] > 0.0
                || (self.values[29] > 0.0 && self.values[25] > 0.0))
    }

    pub fn uniforms(&self) -> &[f32; 40] {
        &self.values
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn motion(expression: ExprId) -> Motion {
        Motion {
            expression,
            blend: None,
            strength: 1.0,
            viseme: crate::viseme::Sample::default(),
            speech: 0.0,
            pupil_kick: 0.0,
            time: 0.0,
            reflected: false,
        }
    }

    #[test]
    fn independent_layers_fade_and_protected_states_clear_them() {
        let mut decor = Decor::default();
        let layers = Layers {
            brows: true,
            ..Layers::default()
        };
        decor.update(layers, Some(motion(ExprId::Joy)), 0.05);
        assert!(decor.moving());
        assert!(decor.uniforms()[6] > 0.0);
        assert_eq!(decor.uniforms()[29], 0.0);
        decor.update(layers, None, 0.01);
        assert!(!decor.active());
    }

    #[test]
    fn stills_use_expression_geometry_and_speech_opens_the_mouth() {
        let layers = Layers {
            brows: true,
            mouth: true,
            details: true,
        };
        let joy = Decor::still(layers, ExprId::Joy, 0.0);
        let surprise = Decor::still(layers, ExprId::Surprise, 0.0);
        assert_ne!(joy.uniforms()[16..30], surprise.uniforms()[16..30]);
        let silent = motion(ExprId::Speaking);
        let speaking = Motion {
            speech: 0.8,
            ..silent
        };
        assert!(Decor::target(layers, speaking)[22] > Decor::target(layers, silent)[22]);
    }
}

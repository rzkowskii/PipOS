//! Short face rehearsals from the Face Lab, run on the native animation clock.

use crate::{anim::Anim, eye::ExprId};
use std::f32::consts::{PI, TAU};
use ExprId::*;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Gesture {
    Blink,
    Nod,
    Shake,
    DoubleTake,
    Giggle,
    Gasp,
    Poke,
    EyeRoll,
    LookAround,
    Jump,
    Wiggle,
    Tremble,
    StartleBurst,
    SceneLookAround,
}

#[derive(Clone, Copy, Default)]
pub struct Movement {
    pub x: f32,
    pub y: f32,
    pub roll: f32,
    pub gaze: [f32; 2],
}

const GESTURES: [(&str, Gesture); 13] = [
    ("blink", Gesture::Blink),
    ("nod", Gesture::Nod),
    ("shake", Gesture::Shake),
    ("double-take", Gesture::DoubleTake),
    ("giggle", Gesture::Giggle),
    ("gasp", Gesture::Gasp),
    ("poke", Gesture::Poke),
    ("eye-roll", Gesture::EyeRoll),
    ("look-around", Gesture::LookAround),
    ("jump", Gesture::Jump),
    ("wiggle", Gesture::Wiggle),
    ("tremble", Gesture::Tremble),
    ("startle-burst", Gesture::StartleBurst),
];

impl Gesture {
    pub const NAMES: &[&str] = &{
        let mut names = [""; GESTURES.len()];
        let mut index = 0;
        while index < GESTURES.len() {
            names[index] = GESTURES[index].0;
            index += 1;
        }
        names
    };

    pub fn from_name(name: &str) -> Option<Self> {
        GESTURES
            .iter()
            .find_map(|&(label, gesture)| (label == name).then_some(gesture))
    }

    pub fn perform(self, anim: &mut Anim) {
        match self {
            Self::Blink => anim.preview_blink(),
            Self::Gasp => anim.preview_flash(Surprise, 0.9),
            Self::Poke => {
                anim.preview_flash(Surprise, 0.7);
                anim.preview_blink();
            }
            Self::StartleBurst => anim.preview_startle_burst(),
            _ => anim.perform_preview(self),
        }
    }

    pub fn motion(self, age: f32) -> Option<Movement> {
        let duration = match self {
            Self::Nod | Self::Shake | Self::Giggle => 0.9,
            Self::DoubleTake => 1.1,
            Self::EyeRoll => 1.3,
            Self::LookAround => 1.8,
            Self::SceneLookAround => 1.4,
            Self::Jump => 0.5,
            Self::Wiggle => 1.0,
            Self::Tremble => 1.2,
            _ => return None,
        };
        if !(0.0..duration).contains(&age) {
            return None;
        }
        let p = age / duration;
        let mut motion = Movement::default();
        match self {
            Self::Nod => {
                let nod = (p * TAU * 2.0).sin() * (1.0 - p);
                motion.y = -0.045 * nod;
                motion.gaze[1] = -0.5 * nod;
            }
            Self::Shake => {
                let shake = (p * TAU * 3.0).sin() * (1.0 - p);
                motion.x = 0.05 * shake;
                motion.gaze[0] = 0.9 * shake;
                motion.roll = 0.05 * shake;
            }
            Self::Giggle => motion.y = (p * TAU * 3.0).sin().abs() * 0.04 * (1.0 - p),
            Self::DoubleTake => {
                motion.gaze[0] = if p < 0.35 {
                    -1.0
                } else if p < 0.6 {
                    1.0
                } else {
                    0.0
                };
            }
            Self::EyeRoll => {
                let (sin, cos) = (p * TAU * 1.1 - PI * 0.5).sin_cos();
                motion.gaze = [1.1 * cos, 0.9 * sin + 0.2];
            }
            Self::LookAround | Self::SceneLookAround => {
                let x = if p < 0.25 {
                    -1.0
                } else if p < 0.6 {
                    1.0
                } else if p < 0.8 {
                    -0.4
                } else {
                    0.0
                };
                motion.gaze = [x, if p < 0.6 { 0.1 } else { 0.0 }];
            }
            Self::Jump => motion.y = (p * PI).sin().abs() * 0.12 * (1.0 - 0.4 * p),
            Self::Wiggle => {
                let swing = (p * TAU * 4.0).sin() * (1.0 - p);
                motion.x = 0.03 * swing;
                motion.roll = 0.10 * swing;
            }
            Self::Tremble => {
                // Sample bounded motion without reseeding or advancing randomness per frame.
                let edge = (age.min(duration - age) / 0.08).min(1.0);
                motion.x = edge * (0.010 * (53.0 * age).sin() + 0.005 * (91.0 * age).sin());
                motion.y = edge * (0.006 * (47.0 * age).sin() + 0.004 * (82.0 * age).sin());
            }
            _ => unreachable!("only timed movement gestures reach this sample"),
        }
        Some(motion)
    }
}

#[derive(Clone, Copy)]
enum Action {
    Blink,
    LookAround,
}

struct Step {
    expression: ExprId,
    seconds: f64,
    action: Option<Action>,
}

const fn pose(expression: ExprId, seconds: f64) -> Step {
    Step {
        expression,
        seconds,
        action: None,
    }
}

impl Step {
    const fn with(mut self, action: Action) -> Self {
        self.action = Some(action);
        self
    }
}

const E16_SECONDS: f64 = 48.0;
const E16_REST_END: f64 = 24.0;
const E16_REST_BLINK: f64 = 60.0 / 17.0;
const E16_TALK_BLINK: f64 = 60.0 / 26.0;

const CATALOGUE: [(&str, &[Step]); 13] = [
    (
        "wake-up",
        &[
            pose(Asleep, 2.2),
            pose(Sleepy, 2.0),
            pose(PWake, 1.6).with(Action::Blink),
            pose(Attentive, 1.2).with(Action::LookAround),
            pose(Joy, 2.2),
        ],
    ),
    (
        "jump-scare",
        &[
            pose(Neutral, 1.4).with(Action::LookAround),
            pose(Surprise, 1.3),
            pose(Scared, 1.8),
            pose(Nervous, 1.6),
            pose(Embarrassed, 1.6),
            pose(Laugh, 2.2),
        ],
    ),
    (
        "heartbreak",
        &[
            pose(Joy, 1.3),
            pose(Love, 1.8),
            pose(Concern, 1.3),
            pose(Sad, 1.6),
            pose(Heartbroken, 2.0),
            pose(Crying, 3.0),
        ],
    ),
    (
        "plot-twist",
        &[
            pose(Thinking, 1.8),
            pose(Idea, 1.4),
            pose(Mischief, 1.6),
            pose(Smug, 1.4),
            pose(Laugh, 2.2),
        ],
    ),
    (
        "meltdown",
        &[
            pose(Annoyed, 1.4),
            pose(Grumpy, 1.4),
            pose(Angry, 1.8),
            pose(Raging, 2.2),
            pose(Dizzy, 1.8),
            pose(Ko, 2.0),
            pose(Dazed, 1.6),
        ],
    ),
    (
        "flirt",
        &[
            pose(Smug, 1.4),
            pose(Flirty, 1.6),
            pose(Wink, 1.4),
            pose(Love, 1.8),
            pose(Bashful, 2.0),
        ],
    ),
    (
        "boot-up",
        &[
            pose(Muted, 0.8),
            pose(Loading, 1.6),
            pose(PGlitch, 1.4),
            pose(PScan, 1.8),
            pose(PSuccess, 1.6),
            pose(Attentive, 1.5),
            pose(Joy, 1.8),
        ],
    ),
    (
        "bad-news",
        &[
            pose(Neutral, 1.0).with(Action::Blink),
            pose(PProcessing, 1.6),
            pose(Concern, 1.6),
            pose(Disappointed, 1.8),
            pose(Resigned, 1.8),
            pose(PDeadpan, 1.6),
        ],
    ),
    (
        "cold-night",
        &[
            pose(Neutral, 1.0),
            pose(Freezing, 2.8),
            pose(Cozy, 2.4),
            pose(Sleepy, 1.8),
            pose(Asleep, 2.5),
        ],
    ),
    // Opaque owner-review labels. All four arms use the same duration, phases
    // and checked expression pairs. `advance_e16` owns their two orthogonal
    // synthetic factors; none of these arms is the shipped face policy.
    ("motion-arm-a", &[pose(Neutral, E16_SECONDS)]),
    ("motion-arm-b", &[pose(Neutral, E16_SECONDS)]),
    ("motion-arm-c", &[pose(Neutral, E16_SECONDS)]),
    ("motion-arm-d", &[pose(Neutral, E16_SECONDS)]),
];

pub const SCENES: &[&str] = &{
    let mut names = [""; CATALOGUE.len()];
    let mut i = 0;
    while i < CATALOGUE.len() {
        names[i] = CATALOGUE[i].0;
        i += 1;
    }
    names
};

fn steps(name: &str) -> Option<&'static [Step]> {
    CATALOGUE
        .iter()
        .find_map(|&(slug, steps)| (slug == name).then_some(steps))
}

pub fn duration(name: &str) -> Option<f64> {
    Some(steps(name)?.iter().map(|step| step.seconds).sum())
}

/// Enter the step containing `now` if its start was crossed. Pass a negative
/// `previous` on the first frame so the opening starts exactly once at zero.
pub fn advance(anim: &mut Anim, name: &str, previous: f64, now: f64) {
    if matches!(name, "motion-arm-a" | "motion-arm-b" | "motion-arm-c" | "motion-arm-d") {
        let conversation_blinks = matches!(name, "motion-arm-b" | "motion-arm-d");
        let smooth_blend = matches!(name, "motion-arm-c" | "motion-arm-d");
        advance_e16(anim, conversation_blinks, smooth_blend, previous, now);
        return;
    }
    let Some(steps) = steps(name) else { return };
    let mut start = 0.0;
    for step in steps {
        let end = start + step.seconds;
        if now < end {
            if previous < start && start <= now {
                anim.audition(step.expression, 1.0, end - now);
                match step.action {
                    Some(Action::Blink) => anim.preview_blink(),
                    Some(Action::LookAround) => anim.perform_preview(Gesture::SceneLookAround),
                    None => {}
                }
            }
            return;
        }
        start = end;
    }
}

fn crossed(previous: f64, now: f64, at: f64) -> bool {
    previous < at && at <= now
}

fn blink_phase(anim: &mut Anim, previous: f64, now: f64, begin: f64, end: f64, interval: f64) {
    let mut at = begin + interval * 0.5;
    while at < end {
        if crossed(previous, now, at) {
            anim.preview_blink();
        }
        at += interval;
    }
}

/// Deterministic WP-E16 owner audition. Arm labels are deliberately opaque.
/// The two synthetic factors are conversation blink cadence and discrete vs
/// smooth checked-pose transitions. No value here is read by the live renderer.
fn advance_e16(
    anim: &mut Anim,
    conversation_blinks: bool,
    smooth_blend: bool,
    previous: f64,
    now: f64,
) {
    if !(0.0..E16_SECONDS).contains(&now) {
        return;
    }
    anim.enable_manual_preview_blinks();

    blink_phase(anim, previous, now, 0.0, E16_REST_END, E16_REST_BLINK);
    blink_phase(
        anim,
        previous,
        now,
        E16_REST_END,
        E16_SECONDS,
        if conversation_blinks { E16_TALK_BLINK } else { E16_REST_BLINK },
    );

    // The first half is genuinely the same neutral resting pose in every arm.
    // The second is explicitly a stylised conversation proxy, not the shipped
    // renderer: alternate valence and arousal pairs every six seconds.
    if now < E16_REST_END {
        anim.audition_blend(Neutral, 1.0, 0.3, None);
        return;
    }
    let conversation = now - E16_REST_END;
    let segment = (conversation / 6.0).floor() as u32;
    let phase = ((conversation % 6.0) / 6.0) as f32;
    let (low, high) = if segment % 2 == 0 {
        (Concern, Joy)
    } else {
        (Sleepy, Surprise)
    };
    let mix = if smooth_blend {
        phase * phase * (3.0 - 2.0 * phase)
    } else if phase < 0.5 {
        0.0
    } else {
        1.0
    };
    anim.audition_blend(low, 1.0, 0.3, Some((high, mix)));
}

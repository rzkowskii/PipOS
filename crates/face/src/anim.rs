//! Eye acting lives here. Expression springs provide the shape, while gaze,
//! blinks and the speech envelope have their own timing.

use crate::{audition, decor, emote};
use crate::eye::{self, ExprId, Eye, Kick, Pose, M};
use crate::pace::Activity;
use std::cell::Cell;
use std::f64::consts::TAU;

/// Small deterministic random source; the face needs no more than this.
struct Rng(u32);

impl Rng {
    fn next(&mut self) -> f32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 17;
        self.0 ^= self.0 << 5;
        (self.0 >> 8) as f32 / (1u32 << 24) as f32
    }

    fn range(&mut self, lo: f32, hi: f32) -> f32 {
        lo + (hi - lo) * self.next()
    }
}

struct Spring {
    x: [f32; M],
    v: [f32; M],
    expression: ExprId,
    intensity: f32,
    blend: Option<(ExprId, f32)>,
    source_pose: bool,
    target: [f32; M],
    settled: bool,
    guarded: bool,
    /// Lively arrivals use a looser spring, so they overshoot a little.
    damping: f32,
    /// Head for the preset reflected left to right (set before a step).
    flip: bool,
    flipped: bool,
}

/// The same expression facing the other way, as when a thought is off to
/// the left rather than the right.
fn reflect(p: Pose) -> Pose {
    Pose { l: p.r.mirror(), r: p.l.mirror() }
}

impl Spring {
    fn new(expression: ExprId) -> Self {
        let target = expression.pose().to_array();
        Self { x: target, v: [0.0; M], expression, intensity: 1.0, blend: None, source_pose: false, target, settled: true, guarded: false,
               damping: eye::DEFAULT_DAMPING, flip: false, flipped: false }
    }

    /// A fresh spring that keeps drawing from the source poses when a preview asked for them.
    fn rebuilt(expression: ExprId, source_pose: bool) -> Self {
        let mut spring = Self::new(expression);
        if source_pose {
            spring.source_pose = true;
            spring.x = expression.concept_pose().to_array();
            spring.target = spring.x;
        }
        spring
    }

    fn step(&mut self, expression: ExprId, intensity: f32, blend: Option<(ExprId, f32)>, dt: f32, omega: f32) {
        if self.expression != expression || self.intensity != intensity || self.blend != blend || self.flip != self.flipped {
            self.guarded |= expression.is_guarded() || intensity != 1.0 || blend.is_some();
            self.expression = expression;
            self.intensity = intensity;
            self.blend = blend;
            self.flipped = self.flip;
            let pose_for = if self.source_pose { ExprId::concept_pose } else { ExprId::pose };
            let pose = pose_for(expression);
            let pose = blend.map_or(pose, |(other, weight)| blend_pose(pose, pose_for(other), weight));
            let pose = if self.flip { reflect(pose) } else { pose };
            self.target = if intensity != 1.0 {
                blend_pose(pose_for(ExprId::Neutral), pose, intensity).to_array()
            } else {
                pose.to_array()
            };
            self.settled = false;
        }
        if self.settled || dt == 0.0 {
            return;
        }
        // Exact underdamped step. Each shape channel shares these coefficients,
        // so a slow frame needs the same work as a fast one.
        let decay = self.damping * omega;
        let frequency = (1.0 - self.damping * self.damping).sqrt() * omega;
        let (sin, cos) = (frequency * dt).sin_cos();
        let envelope = (-decay * dt).exp();
        let travel = envelope * sin / frequency;
        let forward = envelope * cos + decay * travel;
        let backward = envelope * cos - decay * travel;
        let acceleration = -omega * omega * travel;
        self.settled = true;
        for ((x, v), target) in self.x.iter_mut().zip(&mut self.v).zip(self.target) {
            let offset = *x - target;
            let displacement = forward * offset + travel * *v;
            *v = acceleration * offset + backward * *v;
            *x = target + displacement;
            self.settled &= displacement.abs() < 0.00002 && v.abs() < 0.00015;
        }
        if self.settled {
            self.x = self.target;
            self.v = [0.0; M];
            self.guarded = self.expression.is_guarded() || self.intensity != 1.0 || self.blend.is_some();
        }
    }
}

const WAKE_DURATION: f32 = 1.23;
/// The birth ceremony on the very first power-on (task 7.1).
const BIRTH: f32 = 7.6;
/// How long a birth cut short by someone speaking takes to hand over.
const BIRTH_HANDOVER: f32 = 0.4;
/// Where the birth's slow looks begin, after the flicker and the opening.
const BIRTH_LOOKS: f32 = 3.9;
/// A birth asked for while the panel is dark waits this long for it to light.
const BIRTH_PENDING: f64 = 600.0;
/// At most one reunion greeting in this long.
const REUNION_GAP: f64 = 600.0;
/// How long a yawn holds the eyes open before the squeeze.
const YAWN_STRETCH: f32 = 0.9;
const CENTER: [f32; 2] = [0.0, 0.0];
/// How long the "pardon?" squint lasts after words it could not make out.
pub const MISSED_HOLD: f64 = 1.1;

/// Face-only trigger rows: expression and hold in one named entry, so both are
/// cheap to change. Words heard but not made out (`hear.missed` reason `unclear`): confusion.
/// (`no_speech` keeps the skepticism squint of `MISSED_HOLD`.)
const UNCLEAR_HEARING: (&str, f64) = ("confused", 1.1);
/// The charger went in after a low-battery warning had been showing.
const CHARGER_RELIEF: (&str, f64) = ("relieved", 2.0);
/// Heat shows only after this many seconds of continuously fresh hot readings.
const HEAT_SUSTAIN: f64 = 30.0;
/// An armed heat cue ends after this many seconds without a fresh hot reading.
const HEAT_CLEAR: f64 = 60.0;
/// The heat cue (tired) shows this long, then repeats like the battery cue.
const HEAT_CUE_ON: f64 = 6.0;
const HEAT_CUE_EVERY: f64 = 45.0;
/// The longest a budget reading may be trusted, whatever its ttl says.
const HEAT_TTL_CAP: f64 = 12.0;
/// Bodycare republishes about every 10 s against a 12 s ttl, and a busy (hot)
/// laptop can be late. A hot reading that arrives this soon after the last one
/// lapsed still continues the run, so jitter cannot restart the sustain clock.
/// A single stale reading is still far short of `HEAT_SUSTAIN`.
const HEAT_REFRESH_SLACK: f64 = 3.0;

/// The only marks Pip may show by itself in a live conversation, and only
/// while the Effects layer is on: the owner's allow-list. Effect channels are
/// named as in `emote::NAMES`; `cross` (X pupils) and `spiral` (spiral pupils)
/// are the two special pupils. Anything not named here is stripped from every
/// live pose; remove a name here and it disappears everywhere live. Audition
/// and developer previews never read this list. The state-true question mark,
/// thinking dots and battery cue are separate cues and are not governed by it.
const LIVE_MARKS: [&str; 10] =
    ["blush", "hearts", "cry", "sweat", "bubble", "flame", "steam", "snow", "cross", "spiral"];
/// Poses that stay audition-only: no live mark is ever produced for them,
/// whatever an audition or a hold names. (The catalogue flag `is_concept_only`
/// is wider than this; most of those poses may show allowed marks live.)
const LAB_ONLY: [&str; 4] = ["sick", "queasy", "p_sneeze", "p_hiccup"];
/// Flame and steam never show while the body is hot; they ease back in over
/// this many seconds once the hot reading stops being fresh.
const HEAT_MARK_RETURN: f32 = 0.4;
/// X and spiral pupils stay away for this long after unwell, link-lost or
/// loading clear, so the honest cues own the face a moment longer.
const PUPIL_SETTLE: f64 = 5.0;

/// Whether an effect channel is on the live allow-list.
fn live_channel(index: usize) -> bool {
    LIVE_MARKS.contains(&emote::NAMES[index])
}

/// Whether a special pupil (`cross` or `spiral`) is on the live allow-list.
fn live_pupil(name: &str) -> bool {
    LIVE_MARKS.contains(&name)
}

fn ease(t: f32, start: f32, end: f32) -> f32 {
    let x = ((t - start) / (end - start)).clamp(0.0, 1.0);
    x * x * (3.0 - 2.0 * x)
}

fn mix(a: f32, b: f32, amount: f32) -> f32 {
    a + (b - a) * amount
}

fn blend_eye(a: Eye, b: Eye, amount: f32) -> Eye {
    let a = a.to_array();
    let b = b.to_array();
    let blended: [f32; eye::N] = std::array::from_fn(|i| mix(a[i], b[i], amount));
    Eye::from_slice(&blended)
}

/// Close along the eye's local lid curves so even a slanted eye retains its
/// silhouette. The upper lid makes most of the journey toward the crease.
fn close_lids(e: &mut Eye, amount: f32) {
    let take_up = ease(amount, 0.0, 0.23);
    let closure = ((amount - 0.10) / 0.90).clamp(0.0, 1.0);
    let top = mix(e.top, e.top.min(1.05), take_up);
    let bot = mix(e.bot, e.bot.max(-1.05), take_up);
    // Lids that come down straight cut a round eye into a flat-topped cup.
    // Bow them like the eye they cover (holding their middle where it is),
    // so a closing eye narrows to points at its corners and shuts into a
    // soft curve, the way the sleepy crescents look.
    // Only lids that sit clear of the eye are bowed; a lid already shaping
    // it (joy's smiling lower lid, a sleepy upper one) keeps its own curve.
    let clear_top = ease(e.top + e.top_curve, 0.6, 1.0);
    let clear_bot = ease(-(e.bot + e.bot_curve), 0.6, 1.0);
    let arch = (0.85 - e.top_curve).max(0.0) * take_up * clear_top;
    let sag = (0.6 + e.bot_curve).max(0.0) * take_up * clear_bot;
    let (top, bot) = (top - arch, bot + sag);
    e.top_curve += arch;
    e.bot_curve -= sag;
    // Shut, the lids leave a hairline of light of about the same thickness
    // however tall the eye is.
    let sliver = (0.014 / (2.1 * e.h.max(0.01))).min(0.05);
    let upper = closure * 0.74;
    let lower = closure * (0.26 - sliver);
    e.top = mix(top, bot, upper);
    e.bot = mix(bot, top, lower);
    (e.top_slant, e.bot_slant) = (mix(e.top_slant, e.bot_slant, upper), mix(e.bot_slant, e.top_slant, lower));
    (e.top_curve, e.bot_curve) = (mix(e.top_curve, e.bot_curve, upper), mix(e.bot_curve, e.top_curve, lower));
    // A tipped eye (the resting egg leans in) would otherwise shut into a
    // slanted slash that reads as a frown for a frame. As the lids meet,
    // level them on the screen: a closed eye is a calm, level line.
    // The eye turns about the middle of the closing gap, so the line of
    // light stays where it is and only straightens.
    let level = ease(amount, 0.30, 1.0);
    if level > 0.0 && e.tilt != 0.0 {
        let gap = 0.5 * e.h * (e.top + e.top_curve + e.bot + e.bot_curve);
        let tilt = e.tilt * (1.0 - level);
        let (s0, c0) = e.tilt.sin_cos();
        let (s1, c1) = tilt.sin_cos();
        e.x += gap * (s1 - s0);
        e.y += gap * (c0 - c1);
        e.tilt = tilt;
    }
    e.glow *= 1.0 - 0.20 * ease(amount, 0.55, 1.0);
}

fn blink_closure(t: f32, close: f32, hold: f32, open: f32) -> f32 {
    let down = (t / close).clamp(0.0, 1.0);
    (down * (2.0 - down)) * (1.0 - ease(t, close + hold, close + hold + open))
}

struct Blink {
    start: f64,
    close: f32,
    hold: f32,
    open: f32,
    stagger: f32,
    depth: f32,
    repeat: bool,
}

struct BatteryCue {
    next: f64,
    until: f64,
}

/// What the face knows of the body's heat. The brainstem names the pressure;
/// the face keeps no temperatures of its own.
#[derive(Default)]
struct Heat {
    /// The last hot reading is trusted until this time (also set to "now" by
    /// a revoke or a non-hot reading, which makes the freshness end there).
    fresh_until: f64,
    /// When the current unbroken run of fresh hot readings began.
    since: Option<f64>,
    /// Sustained long enough: the cue runs until `HEAT_CLEAR` of no hot reading.
    cue: Option<BatteryCue>,
}

impl Blink {
    fn elapsed(&self, now: f64) -> f32 {
        (now - self.start) as f32
    }

    fn closure(&self, now: f64, side: f32) -> f32 {
        let t = self.elapsed(now) - (side * self.stagger).max(0.0);
        self.depth * blink_closure(t, self.close, self.hold, self.open)
    }

    fn done(&self, now: f64) -> bool {
        self.elapsed(now) >= self.close + self.hold + self.open + self.stagger.abs()
    }
}

fn distance(a: [f32; 2], b: [f32; 2]) -> f32 {
    (a[0] - b[0]).abs().max((a[1] - b[1]).abs())
}

/// An interruptible movement between two fixations. A held gaze is still,
/// and a small correction arrives faster than a glance across the face.
struct Gaze {
    from: [f32; 2],
    to: [f32; 2],
    start: f64,
    duration: f32,
}

impl Gaze {
    fn new() -> Self {
        Self { from: CENTER, to: CENTER, start: 0.0, duration: 0.08 }
    }

    fn position(&self, now: f64) -> [f32; 2] {
        let progress = ease((now - self.start) as f32, 0.0, self.duration);
        std::array::from_fn(|i| mix(self.from[i], self.to[i], progress))
    }

    /// `unhurried` gives a slow look around the room the pace of a head turn.
    fn track(&mut self, target: [f32; 2], now: f64, unhurried: f32) -> [f32; 2] {
        let current = self.position(now);
        if target != self.to {
            self.from = current;
            self.to = target;
            self.start = now;
            self.duration = (0.070 + 0.095 * distance(self.from, target).min(1.35).sqrt()).max(unhurried);
        }
        current
    }
}

struct LidWake {
    started: f64,
    dreamed: bool,
}

struct Birth {
    started: f64,
    /// Someone spoke to it: hand over to the live face from here.
    cut: Option<f64>,
}

/// The slow internal numbers the brainstem publishes as `body.drives`, each
/// 0 to 1 (absent when the body cannot tell).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Drives {
    /// Battery charge (1 on mains power).
    pub energy: Option<f32>,
    /// Recent stimulation: falls over about a minute of quiet.
    pub arousal: Option<f32>,
    /// Recent company: falls over about an hour alone.
    pub social: Option<f32>,
    /// Cool enough: falls as the body heats past 50 C.
    pub comfort: Option<f32>,
}

impl Drives {
    /// Lid weight from a flat battery or a hot body, alone at idle. It stays
    /// under the yawn threshold: tiredness is not sleepiness.
    fn tiredness(self) -> f32 {
        bus::body_drives::tiredness(self.energy, self.comfort)
    }

    /// How long idle looks linger: slower after a long quiet, brisker just
    /// after something happened.
    fn gaze_tempo(self) -> f32 {
        self.arousal.map_or(1.0, |a| mix(1.4, 0.85, a.clamp(0.0, 1.0)))
    }

    /// How keen it is to see someone again: zero after recent company,
    /// rising after about half an hour alone.
    fn eagerness(self) -> f32 {
        bus::body_drives::eagerness(self.social)
    }
}

/// What the eyes do while an app panel is open (voice-apps.md section 3.2).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum FocusKind {
    /// Pip is typing: follow the cursor with small reading saccades.
    Typing,
    /// A job is running: watch the newest output.
    Watching,
    /// Pip paused: look up at the person and hold.
    Paused,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Focus {
    pub kind: FocusKind,
    /// Where on the panel, as a gaze target.
    pub at: [f32; 2],
}

/// The lamp-warming flicker of a first power-on: brightness steps.
fn flicker(t: f32) -> f32 {
    const STEPS: [(f32, f32); 12] = [
        (0.0, 0.0),
        (0.35, 0.55),
        (0.42, 0.0),
        (0.80, 0.0),
        (0.86, 0.8),
        (0.93, 0.25),
        (1.18, 0.0),
        (1.26, 0.9),
        (1.33, 0.45),
        (1.52, 1.0),
        (1.58, 0.7),
        (1.80, 1.0),
    ];
    STEPS.iter().rev().find(|(at, _)| t >= *at).map_or(0.0, |&(_, v)| v)
}

/// The first power-on: a line of light that flickers on, opens slowly,
/// looks slowly one way and the other, tilts curiously, and settles into
/// the live face (`ready`).
fn birth_pose(t: f32, ready: Pose) -> Pose {
    let sleepy = ExprId::Sleepy.pose();
    let curious = ExprId::Curiosity.pose();
    let line = Eye { h: 0.016 + 0.012 * ease(t, 0.3, 2.4), glow: 0.42 * flicker(t), ..Eye::OPEN };
    let lines = Pose::sym(line);
    let mut out = [lines.l, lines.r];
    let readies = [ready.l, ready.r];
    let sleepies = [sleepy.l, sleepy.r];
    let curiouses = [curious.l, curious.r];
    let look = -ease(t, 3.9, 4.7) * (1.0 - ease(t, 4.9, 5.6)) + ease(t, 5.5, 6.2) * (1.0 - ease(t, 6.3, 7.0));
    let tilt = ease(t, 6.5, 6.95) * (1.0 - ease(t, 7.05, BIRTH));
    let head = -0.08 * tilt;
    for (i, e) in out.iter_mut().enumerate() {
        let lag = 0.1 * i as f32;
        let opened = blend_eye(*e, sleepies[i], ease(t, 2.4 + lag, 3.1 + lag));
        *e = blend_eye(opened, readies[i], ease(t, 3.0 + lag, 3.9 + lag));
        *e = blend_eye(*e, curiouses[i], 0.55 * tilt);
        e.x += 0.30 * look;
        e.y -= 0.03 * look.abs();
        e.tilt += 0.05 * look;
        // The near eye grows a little as it turns, as a real gaze does.
        let near = 1.0 + 0.06 * (2.0 * i as f32 - 1.0) * look;
        e.w *= near;
        e.h *= near;
        // One slow blink between the two looks.
        close_lids(e, blink_closure(t - 5.05, 0.13, 0.07, 0.22));
        if head != 0.0 {
            let (sin, cos) = head.sin_cos();
            (e.x, e.y) = (e.x * cos - e.y * sin, e.x * sin + e.y * cos);
            e.tilt += head;
        }
    }
    Pose { l: out[0], r: out[1] }
}

fn blend_pose(a: Pose, b: Pose, amount: f32) -> Pose {
    Pose { l: blend_eye(a.l, b.l, amount), r: blend_eye(a.r, b.r, amount) }
}

fn dream_flutter(eye: &mut Eye, amount: f32) {
    eye.x += 0.022 * amount;
    eye.top += 0.075 * amount;
    eye.glow *= 1.0 + 0.07 * amount;
}

/// A small decaying hop: |sin| arcs that lose height as they go.
fn hop(age: f32, period: f32, count: f32) -> f32 {
    if age <= 0.0 || age >= period * count {
        return 0.0;
    }
    (std::f32::consts::PI * age / period).sin().abs() * (-age / period).exp()
}

/// A quick tuck before a lively move and its release, peaking at 1.
fn anticipation(age: f32) -> f32 {
    ease(age, 0.0, 0.05) * (1.0 - ease(age, 0.05, 0.15))
}

fn pulse(age: f32, start: f32, peak: f32, end: f32) -> f32 {
    ease(age, start, peak) * (1.0 - ease(age, peak, end))
}

/// Small self-contained performances layered over the expression.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Gesture {
    /// A soft, pleased half-close of the lids with a smiling lower lid.
    Content,
    /// A stretch up, a scrunched squeeze, and a slow release.
    Yawn,
    Nod,
    Acknowledge,
    Shake,
}

impl Gesture {
    fn length(self) -> f32 {
        match self {
            Gesture::Content => 2.3,
            Gesture::Yawn => 2.6,
            Gesture::Nod => 0.68,
            Gesture::Acknowledge => 0.56,
            Gesture::Shake => 0.82,
        }
    }
}

/// One frame's worth of acting, worked out once and applied to each eye.
struct Life {
    tuck: f32,
    bounce: f32,
    pop: f32,
    recoil: f32,
    nod: f32,
    shake: f32,
    content: f32,
    inhale: f32,
    squeeze: f32,
    /// The joy pose, looked up only while a contented gesture borrows its lids.
    smile: Option<Pose>,
}

impl Life {
    fn apply(&self, e: &mut Eye, side: f32, heaviness: f32, head: f32) {
        let Life { tuck, bounce, pop, recoil, nod, shake, content, inhale, squeeze, smile } = *self;
        // The big breath of a yawn opens even heavy lids wide for a moment.
        let heaviness = heaviness * (1.0 - 0.85 * inhale);
        // A tuck before a lively move, hops of delight, and the surprise pop.
        e.h *= 1.0 - 0.06 * tuck + 0.7 * bounce + 0.09 * pop + 0.17 * inhale;
        e.w *= 1.0 + 0.025 * tuck - 0.25 * bounce + 0.06 * pop - 0.05 * inhale;
        e.y += bounce - 0.008 * tuck + 0.04 * inhale - 0.015 * squeeze + 0.012 * content - 0.012 * heaviness;
        e.glow *= (1.0 + 1.2 * bounce + 0.08 * pop + 0.06 * inhale) * (1.0 - 0.10 * squeeze) * (1.0 - 0.12 * heaviness);
        if recoil != 0.0 || nod != 0.0 || shake != 0.0 {
            e.x += 0.11 * shake;
            e.y += nod - 0.047 * recoil;
            e.h *= 1.0 - 0.09 * recoil + 0.3 * nod;
            e.w *= 1.0 - 0.05 * recoil + 0.035 * side * shake;
            e.tilt += 0.035 * shake;
        }
        if let Some(smile) = smile {
            // A pleased squint: borrow joy's smiling lower lid part of the way.
            let joy = if side < 0.0 { smile.l } else { smile.r };
            let amount = 0.70 * content;
            for (lid, happy) in [
                (&mut e.top, joy.top),
                (&mut e.top_slant, joy.top_slant),
                (&mut e.top_curve, joy.top_curve),
                (&mut e.bot, joy.bot),
                (&mut e.bot_slant, joy.bot_slant),
                (&mut e.bot_curve, joy.bot_curve),
                (&mut e.round, joy.round),
                (&mut e.tilt, joy.tilt),
            ] {
                *lid = mix(*lid, happy, amount);
            }
            close_lids(e, 0.18 * content);
        }
        if squeeze > 0.0 {
            e.bot_curve += 0.35 * squeeze;
            e.tilt -= side * 0.04 * squeeze;
            close_lids(e, 0.86 * squeeze);
        }
        if heaviness > 0.0 {
            // Fade the lids in from nothing, so the first trace of
            // heaviness (or the last, as company lifts it) never pops.
            let amount = (0.1 + 0.8 * heaviness) * ease(heaviness, 0.0, 0.08);
            // Half-closed over the inward-tipped resting egg, the lids would
            // slope down toward the nose and read as a scowl. A drowsy eye
            // relaxes level instead, its lid sagging toward the outside.
            e.tilt *= 1.0 - 0.8 * (2.0 * amount).min(1.0);
            e.top_slant -= side * 0.3 * amount;
            close_lids(e, amount);
        }
        if head != 0.0 {
            let (sin, cos) = head.sin_cos();
            (e.x, e.y) = (e.x * cos - e.y * sin, e.x * sin + e.y * cos);
            e.tilt += head;
        }
    }
}

/// The lid wake plays once, regardless of which organs are ready.
fn lid_wake_pose(t: f32, ready: Pose, dreamed: bool) -> Pose {
    let closed = Pose::sym(Eye { h: 0.016, glow: 0.34, ..Eye::OPEN });
    let sleepy = ExprId::Sleepy.pose();
    let curious = ExprId::Curiosity.pose();
    let neutral = ExprId::Neutral.pose();
    let mut l = blend_eye(closed.l, sleepy.l, ease(t, 0.09, 0.44));
    let mut r = blend_eye(closed.r, sleepy.r, ease(t, 0.18, 0.53));

    // Briefly look out from under the lids, then find the person on the
    // other side of the screen as the blink lifts.
    let peer = ease(t, 0.30, 0.48) * (1.0 - ease(t, 0.69, 0.88));
    let perk = ease(t, 0.68, 0.88);
    l = blend_eye(l, blend_eye(neutral.l, curious.l, 0.64), perk);
    r = blend_eye(r, blend_eye(neutral.r, curious.r, 0.56), perk);
    l.x -= 0.052 * peer;
    r.x -= 0.052 * peer;
    l.y -= 0.018 * peer;
    r.y -= 0.018 * peer;
    close_lids(&mut l, blink_closure(t - 0.605, 0.080, 0.042, 0.154));
    close_lids(&mut r, blink_closure(t - 0.618, 0.080, 0.042, 0.168));
    if dreamed {
        // A completed batch gets a brief flutter in the waking blink.
        for (age, eye) in [(t, &mut l), (t - 0.028, &mut r)] {
            let flutter = ease(age, 0.68, 0.755) * (1.0 - ease(age, 0.765, 0.91));
            dream_flutter(eye, flutter);
        }
    }

    let settle = ease(t, 0.90, WAKE_DURATION);
    Pose { l: blend_eye(l, ready.l, settle), r: blend_eye(r, ready.r, settle) }
}

struct HeldExpression {
    id: ExprId,
    until: f64,
    intensity: f32,
    blend: Option<(ExprId, f32)>,
}

pub struct Anim {
    t: f64,
    rng: Rng,
    spring: Spring,
    omega: f32,

    state: String,
    state_at: f64,
    reply_ladder: bool,
    speech_end: Option<f64>,
    muted: bool,
    unwell: bool,
    /// The brainstem has gone quiet; the face shows it without being told.
    link_lost: bool,
    emotion: Option<HeldExpression>,
    speech_cue: Option<ExprId>,
    speech_ended: bool,
    screen_concern_until: f64,
    unheard_until: f64,
    battery_cue: Option<BatteryCue>,
    /// Sustained thermal pressure from `stem.budget`; its cue is a tired look
    /// on the battery cue's schedule.
    heat: Heat,
    preview_dream: bool,
    concept_preview: bool,
    preview_effects: [bool; emote::CHANNELS],
    /// The Effects layer is on: forced by `set_effects_layer` (tests), or seen
    /// from the render loop asking for emotes (`effects_called_t`).
    effects_layer: bool,
    /// When the render loop last asked for live emotes. It only does so with
    /// the Effects layer on and room for it, so this is how the face learns the
    /// layer state without the loop having to tell it.
    effects_called_t: Cell<f64>,
    /// The last time unwell, link-lost or loading was seen (X/spiral guard).
    pupil_blocked_at: f64,
    preview_flash: Option<(ExprId, f64)>,
    preview_burst: Option<f64>,
    preview_gesture: Option<(audition::Gesture, f64)>,
    preview_gaze: [f32; 2],
    lid_open: bool,
    panel_blank: bool,
    lid_wake: Option<LidWake>,

    gaze: Gaze,
    gaze_bias: [f32; 2],
    fixation: [f32; 2],
    gaze_next: f64,
    gaze_away: bool,
    idle_glances: bool,
    thought_side: f32,
    presence: Option<([f32; 2], f64)>,
    touch: Option<([f32; 2], f64)>,

    blink: Option<Blink>,
    blink_next: f64,
    blink_repeat: bool,
    /// Offline audition scenes may own every blink for measured cadence.
    preview_blink_manual: bool,
    /// Kept in f64 and advanced by real elapsed time, so breathing stays
    /// smooth after days awake and in step after a slow frame.
    breath_phase: f64,
    breath_depth: f32,
    breath_scale: f32,
    breath_experiment: bool,
    breath_effort: f32,
    breath_effort_target: f32,

    level: f32,
    level_target: f32,
    level_at: f64,

    /// The expression last seen arriving, where it came from, and when.
    acting: ExprId,
    acting_intensity: f32,
    acted_from: ExprId,
    acted_at: f64,
    /// The next blink is a quick double, as after a surprise.
    double_blink: bool,
    startle_followup: bool,
    /// A whole-face roll (radians), as if the head tilted.
    head: f32,
    head_v: f32,
    listen_side: f32,
    gesture: Option<(Gesture, f64, f32)>,
    gesture_next: f64,
    /// Quiet time makes the lids heavy; any company lifts them at once.
    quiet_since: f64,
    heaviness: f32,
    yawned: bool,
    /// Thinking searches around one spot in small hops.
    thought_spot: [f32; 2],
    search_next: f64,
    /// The current idle look is a slow look around the room.
    surveying: bool,

    drives: Drives,
    /// The next attentive arrival is a reunion: a bigger hop.
    reunion: f32,
    reunited_at: f64,
    birth: Option<Birth>,
    /// A birth asked for while dark, and when.
    birth_pending: Option<f64>,
    /// When the panel last lit up.
    lit_at: f64,
    /// No brainstem yet at power-on: a calm waiting look instead of dazed.
    loading: bool,
    focus: Option<Focus>,
    /// The cursor point the eyes are heading for, and when they get there.
    focus_seen: [f32; 2],
    focus_next: f64,
    /// Pip is typing with the terminal minimised: no idle performances.
    typing_hidden: bool,
}

impl Anim {
    /// Starts with the eyes closed, so the first thing it does is wake.
    pub fn new(seed: u32) -> Anim {
        Anim {
            t: 0.0,
            rng: Rng(seed | 1),
            spring: Spring::new(ExprId::Asleep),
            omega: 2.5,
            state: "waking".to_string(),
            state_at: 0.0,
            reply_ladder: false,
            speech_end: None,
            muted: false,
            unwell: false,
            link_lost: false,
            emotion: None,
            speech_cue: None,
            speech_ended: true,
            screen_concern_until: 0.0,
            unheard_until: 0.0,
            battery_cue: None,
            heat: Heat::default(),
            preview_dream: false,
            concept_preview: false,
            preview_effects: [false; emote::CHANNELS],
            effects_layer: false,
            effects_called_t: Cell::new(f64::NEG_INFINITY),
            pupil_blocked_at: f64::NEG_INFINITY,
            preview_flash: None,
            preview_burst: None,
            preview_gesture: None,
            preview_gaze: CENTER,
            lid_open: false,
            panel_blank: false,
            lid_wake: None,
            gaze: Gaze::new(),
            gaze_bias: CENTER,
            fixation: CENTER,
            gaze_next: 2.2,
            gaze_away: false,
            idle_glances: true,
            thought_side: 1.0,
            presence: None,
            touch: None,
            blink: None,
            blink_next: 3.0,
            blink_repeat: false,
            preview_blink_manual: false,
            breath_phase: 0.0,
            breath_depth: 0.0,
            breath_scale: 1.0,
            breath_experiment: false,
            breath_effort: 0.0,
            breath_effort_target: 0.0,
            level: 0.0,
            level_target: 0.0,
            level_at: -10.0,
            acting: ExprId::Asleep,
            acting_intensity: 1.0,
            acted_from: ExprId::Asleep,
            acted_at: -10.0,
            double_blink: false,
            startle_followup: false,
            head: 0.0,
            head_v: 0.0,
            listen_side: 1.0,
            gesture: None,
            gesture_next: 16.0,
            quiet_since: 0.0,
            heaviness: 0.0,
            yawned: false,
            thought_spot: CENTER,
            search_next: 0.0,
            surveying: false,
            drives: Drives::default(),
            reunion: 0.0,
            reunited_at: f64::NEG_INFINITY,
            birth: None,
            birth_pending: None,
            lit_at: 0.0,
            loading: false,
            focus: None,
            focus_seen: CENTER,
            focus_next: 0.0,
            typing_hidden: false,
        }
    }

    /// The very first power-on (`life.woke` with `first`). Plays now if the
    /// panel is lit, else on the next lighting within ten minutes.
    pub fn born(&mut self) {
        if self.panel_blank {
            self.birth_pending = Some(self.t);
        } else if self.birth.is_none() {
            // Eyes that have been up a while do not drop back to a dark line:
            // they go straight to the first slow looks.
            let skip = if self.t - self.lit_at > 1.5 { BIRTH_LOOKS } else { 0.0 };
            self.start_birth(skip);
        }
    }

    fn start_birth(&mut self, skip: f32) {
        self.birth_pending = None;
        self.birth = Some(Birth { started: self.t - f64::from(skip), cut: None });
        self.lid_wake = None;
        self.blink = None;
        self.blink_repeat = false;
        self.double_blink = false;
        self.gesture = None;
        self.blink_next = self.t + f64::from(BIRTH) + 0.8;
        self.quiet_since = self.t;
    }

    #[cfg(test)]
    pub fn being_born(&self) -> bool {
        self.birth.is_some()
    }

    pub fn set_drives(&mut self, drives: Drives) {
        self.drives = drives;
    }

    pub fn enable_effort_breathing(&mut self) {
        self.breath_experiment = true;
    }

    pub fn set_breath_effort(&mut self, effort: Option<f32>) {
        self.breath_effort_target = effort.unwrap_or(0.0);
    }

    /// The brainstem has never answered since the face started on the
    /// appliance: look like waiting, not like illness.
    pub fn set_loading(&mut self, loading: bool) {
        self.loading = loading;
    }

    /// The app panel's focus, or None with no panel shown.
    pub fn set_focus(&mut self, focus: Option<Focus>) {
        if focus.map(|f| f.kind) != self.focus.map(|f| f.kind) {
            self.focus_next = self.t;
            if focus.is_none() {
                self.reset_fixation();
            }
        }
        self.focus = focus;
    }

    pub fn set_typing_hidden(&mut self, on: bool) {
        self.typing_hidden = on;
    }

    pub fn enable_concept_preview(&mut self, effects: [bool; emote::CHANNELS], source_pose: bool) {
        self.concept_preview = true;
        self.preview_effects = effects;
        self.spring.source_pose = source_pose;
        if source_pose {
            self.spring.x = self.spring.expression.concept_pose().to_array();
            self.spring.target = self.spring.x;
        }
    }

    pub fn preview_flash(&mut self, expression: ExprId, seconds: f64) {
        self.preview_flash = Some((expression, self.t + seconds));
    }

    pub fn preview_startle_burst(&mut self) {
        self.preview_burst = Some(self.t);
    }

    pub fn perform_preview(&mut self, gesture: audition::Gesture) {
        self.preview_gesture = Some((gesture, self.t));
    }

    pub fn preview_blink(&mut self) {
        self.blink = Some(Blink {
            start: self.t, close: 0.075, hold: 0.035, open: 0.12,
            stagger: 0.0, depth: 1.0, repeat: false,
        });
    }

    pub fn enable_manual_preview_blinks(&mut self) {
        self.preview_blink_manual = true;
        self.blink_repeat = false;
        self.double_blink = false;
    }

    /// How fast the face needs drawing right now (task 2.1).
    pub fn activity(&self) -> Activity {
        if self.panel_blank {
            return Activity::Blank;
        }
        let gaze_moving = ((self.t - self.gaze.start) as f32) < self.gaze.duration;
        let busy = self.lid_wake.is_some()
            || self.birth.is_some()
            || self.blink.is_some()
            || self.gesture.is_some()
            || self.preview_gesture.is_some()
            || self.preview_gaze != CENTER
            || !self.spring.settled
            || self.level > 0.0
            || self.touch.is_some()
            || gaze_moving
            || self.head_v.abs() > 0.002
            || (self.acting.is_guarded() && self.acting.motion().kick != Kick::None && self.t - self.acted_at < 0.86)
            || (self.concept_preview && !self.dazed() && !self.muted && self.acting_intensity > 0.0
                && (self.preview_effect_values(self.acting)[emote::SHIVER..=emote::SWAY].iter().any(|value| *value > 0.0)
                    || (self.acting.is_concept_only() && self.acting.visual().kick.pop > 0.0 && self.t - self.acted_at < 0.7)))
            || (self.engaged() && !self.muted)
            || (!self.asleep() && self.live_marks().is_some());
        if busy {
            Activity::Active
        } else if self.asleep() {
            Activity::Asleep
        } else {
            Activity::Calm
        }
    }

    /// The optional idle pacing experiment never slows an expression or a turn.
    pub fn trial_idle(&self) -> bool {
        self.state == "idle" && self.emotion.is_none() && !self.dazed() && self.activity() == Activity::Calm
    }

    pub fn enable_reply_ladder(&mut self) {
        self.reply_ladder = true;
    }

    /// Start only from a newly accepted utterance. A recognizer that takes
    /// longer than a stage skips that stage instead of replaying it late.
    fn acknowledge_speech(&mut self, age_ms: u64) {
        if !self.reply_ladder || age_ms >= 2000 || self.state != "thinking"
            || self.panel_blank || self.muted || self.dazed() || self.speech_end.is_some()
        {
            return;
        }
        self.speech_end = Some(self.t - age_ms as f64 / 1000.0);
        self.thought_spot = [0.57 * self.thought_side, 0.44];
        if age_ms <= 250 && self.blink.is_none() {
            self.blink_next = self.t;
        }
    }

    pub fn set_state(&mut self, state: &str, muted: bool, unwell: bool) {
        self.set_state_at_speech_age(state, muted, unwell, None);
    }

    pub fn set_state_at_speech_age(&mut self, state: &str, muted: bool, unwell: bool, speech_age: Option<u64>) {
        self.preview_dream = false;
        let changed = state != self.state;
        let heard = self.state == "listening" && state == "thinking";
        if changed {
            self.speech_end = None;
            if state == "waking" && self.asleep() {
                self.omega = 2.5;
            }
            if self.state == "speaking" {
                self.end_reply();
            }
            if state == "speaking" {
                self.speech_ended = false;
            }
            if matches!(state, "attending" | "listening" | "speaking") {
                self.screen_concern_until = 0.0;
            }
            if state == "asleep" {
                self.touch = None;
                self.presence = None;
                self.emotion = None;
                self.blink_repeat = false;
                self.double_blink = false;
                if let Some(blink) = &mut self.blink {
                    blink.repeat = false;
                }
            }
            if state == "thinking" {
                self.thought_side = if self.rng.next() < 0.5 { -1.0 } else { 1.0 };
            }
            if state == "listening" {
                self.listen_side = if self.rng.next() < 0.5 { -1.0 } else { 1.0 };
            }
            // A yawn may carry on into sleep; any other gesture yields to the news.
            let yawning = self.gesture.is_some_and(|(g, _, _)| g == Gesture::Yawn);
            if !(yawning && state == "asleep") {
                self.gesture = None;
            }
            if state == "drowsy" && !muted && !unwell && !self.link_lost && !self.panel_blank {
                self.start_gesture(Gesture::Yawn);
            }
            // Someone it has not seen for a long while: a keener hello.
            self.reunion = 0.0;
            let from_rest = matches!(self.state.as_str(), "idle" | "drowsy" | "waking" | "asleep");
            if state == "attending" && from_rest && !muted && !unwell {
                let eager = self.drives.eagerness();
                if eager > 0.0 && self.t - self.reunited_at >= REUNION_GAP {
                    self.reunion = eager;
                    self.reunited_at = self.t;
                }
            }
            if matches!(state, "attending" | "listening" | "speaking") {
                if let Some(birth) = &mut self.birth {
                    birth.cut.get_or_insert(self.t);
                }
            }
            if state == "idle" {
                self.gesture_next = self.t + f64::from(self.rng.range(12.0, 24.0));
            }
            self.quiet_since = self.t;
            self.yawned = false;
            self.state.clear();
            self.state.push_str(state);
            self.state_at = self.t;
        }
        let status_changed = muted != self.muted || unwell != self.unwell;
        self.muted = muted;
        self.unwell = unwell;
        if muted || unwell || self.asleep() {
            self.hush_reply();
            self.speech_end = None;
            self.screen_concern_until = 0.0;
            self.unheard_until = 0.0;
        }
        if heard && self.layers_visible() && !(self.reply_ladder && speech_age.is_some()) {
            self.start_gesture(Gesture::Acknowledge);
        }
        if changed || status_changed {
            self.reset_fixation();
            // Give the first visible look on acknowledging someone time to land.
            if self.engaged() {
                self.blink_next = self.blink_next.max(self.t + 0.9);
                self.blink_repeat = false;
            }
        }
        if changed {
            if let Some(age) = speech_age {
                self.acknowledge_speech(age);
            }
        }
    }

    fn engaged(&self) -> bool {
        matches!(self.state.as_str(), "attending" | "listening" | "speaking")
    }

    fn follows_presence(&self) -> bool {
        !self.muted && !self.unwell && !self.link_lost && (self.engaged() || self.state == "idle")
    }

    fn reset_fixation(&mut self) {
        self.gaze_away = false;
        self.surveying = false;
        self.gaze_next = self.t;
        if self.state == "idle" && self.presence.is_none() {
            self.fixation = CENTER;
            self.gaze_next += f64::from(self.rng.range(2.4, 4.5));
        } else if self.state == "thinking" && !self.muted && !self.dazed() {
            self.gaze_next += 0.12;
        }
    }

    pub fn express(&mut self, name: &str, hold: f64) {
        self.express_with(name, hold, None, None);
    }

    /// `express` with the optional strength and blend of `face.express`.
    /// `intensity` is clamped to 0..1 and ignored (full strength) when it is
    /// not finite. `blend` is a public name and a mix; a bad name, the main
    /// pose's own name or a non-finite mix drops the blend and the pose plays
    /// alone, and a mix is clamped to 0..1.
    pub fn express_event(&mut self, name: &str, hold: f64, intensity: Option<f64>, blend: Option<&serde_json::Value>) {
        // The blend arrives as untrusted JSON: only an object with a string name and a
        // numeric mix counts, and anything else plays the pose alone.
        let blend = blend.and_then(|value| {
            let object = value.as_object()?;
            Some((object.get("name")?.as_str()?, object.get("mix")?.as_f64()?))
        });
        self.express_with(name, hold, intensity, blend);
    }

    pub fn express_with(&mut self, name: &str, hold: f64, intensity: Option<f64>, blend: Option<(&str, f64)>) {
        let public = |name: &str| {
            ExprId::PUBLIC.iter().copied().find(|id| id.name() == name).filter(|id| *id != ExprId::Dreaming)
        };
        let Some(expression) = public(name) else {
            return;
        };
        let intensity = intensity.filter(|v| v.is_finite()).map_or(1.0, |v| v.clamp(0.0, 1.0)) as f32;
        let blend = blend.and_then(|(other, mix)| {
            let other = public(other).filter(|id| *id != expression)?;
            mix.is_finite().then(|| (other, mix.clamp(0.0, 1.0) as f32))
        });
        self.audition_blend(expression, intensity, hold, blend);
    }

    /// Show a typed pose from the local visual audition, with its strength
    /// measured from the neutral pose. Live model expressions arrive at full strength.
    pub fn audition(&mut self, id: ExprId, intensity: f32, hold: f64) {
        self.audition_blend(id, intensity, hold, None);
    }

    pub fn audition_blend(&mut self, id: ExprId, intensity: f32, hold: f64, blend: Option<(ExprId, f32)>) {
        if self.panel_blank || self.asleep() || !intensity.is_finite() || !hold.is_finite() {
            return;
        }
        self.emotion = Some(HeldExpression {
            id, until: self.t + hold.clamp(0.2, 600.0), intensity: intensity.clamp(0.0, 1.5), blend,
        });
        self.quiet_since = self.t;
        if self.gesture.is_some_and(|(g, _, _)| g == Gesture::Content) {
            self.gesture = None;
        }
    }

    /// Preview the sleeping flutter while the appliance would be dark.
    pub fn preview_dreaming(&mut self) {
        self.set_state("asleep", false, false);
        self.preview_dream = true;
        self.state_at = self.t;
    }

    /// `on_ac` is the charger reading (`None` when unknown). Leaving the low
    /// state with the charger in is a relief; unplugging or an unknown
    /// charger is not.
    pub fn set_low_battery(&mut self, low: bool, on_ac: Option<bool>) {
        if low {
            self.battery_cue.get_or_insert(BatteryCue { next: self.t, until: self.t });
        } else if self.battery_cue.take().is_some() && on_ac == Some(true) {
            self.express(CHARGER_RELIEF.0, CHARGER_RELIEF.1);
        }
    }

    /// A `stem.budget` readout. Only a hot or critical reading whose priority
    /// is thermal counts as heat; a revoke (`active` false), another priority
    /// or any cooler pressure counts as "not hot" and ends the reading's
    /// freshness at once. The brainstem sends one budget at a time, so a
    /// reading for another priority replaces a thermal one.
    pub fn thermal_budget(&mut self, pressure: Option<&str>, priority: Option<&str>, active: bool, ttl_ms: f64) {
        self.step_heat();
        let hot = active && priority == Some("thermal") && matches!(pressure, Some("hot" | "critical"));
        if hot {
            if self.heat.since.is_none() || self.t >= self.heat.fresh_until + HEAT_REFRESH_SLACK {
                self.heat.since = Some(self.t);
            }
            let ttl = (ttl_ms / 1000.0).clamp(0.001, HEAT_TTL_CAP);
            self.heat.fresh_until = self.t + if ttl.is_finite() { ttl } else { 0.001 };
        } else {
            // A revoke or a cooler reading ends the run at once, slack or not.
            self.heat.fresh_until = self.heat.fresh_until.min(self.t);
            self.heat.since = None;
        }
        self.step_heat();
    }

    /// Arm the heat cue after `HEAT_SUSTAIN` of unbroken freshness; drop it
    /// after `HEAT_CLEAR` without a fresh hot reading. Freshness is judged on
    /// this clock, so a reading whose ttl lapsed counts as not hot.
    fn step_heat(&mut self) {
        let fresh = self.t < self.heat.fresh_until;
        if fresh {
            if self.heat.cue.is_none() && self.heat.since.is_some_and(|since| self.t - since >= HEAT_SUSTAIN) {
                self.heat.cue = Some(BatteryCue { next: self.t, until: self.t });
            }
        } else {
            if self.t >= self.heat.fresh_until + HEAT_REFRESH_SLACK {
                self.heat.since = None;
            }
            if self.heat.cue.is_some() && self.t - self.heat.fresh_until >= HEAT_CLEAR {
                self.heat.cue = None;
            }
        }
    }

    /// The tired look of a low battery or sustained heat is showing. A low
    /// battery takes precedence: while it is flagged the heat cue stays
    /// silent, so the two never double up or restart each other's timing.
    fn tired_cue_showing(&self) -> bool {
        let showing = |cue: &Option<BatteryCue>| cue.as_ref().is_some_and(|cue| self.t < cue.until);
        if self.battery_cue.is_some() { showing(&self.battery_cue) } else { showing(&self.heat.cue) }
    }

    pub fn end_reply(&mut self) {
        self.hush_reply();
        self.speech_ended = true;
    }

    /// Drop the reply's cue and level without ending it. A link blip, a dark
    /// panel or a mute can pass while the reply carries on, and its later
    /// levels must still reach the eyes.
    pub fn hush_reply(&mut self) {
        self.speech_cue = None;
        if matches!(self.state.as_str(), "thinking" | "speaking") {
            self.emotion = None;
            self.level_target = 0.0;
        }
    }

    pub fn set_gaze(&mut self, x: f32, y: f32) {
        if self.panel_blank || self.asleep() {
            return;
        }
        let measured = [x.clamp(-1.0, 1.0), y.clamp(-1.0, 1.0)];
        let mut point = measured;
        if let Some((old, _)) = self.presence {
            // Keep camera jitter from pulling a held fixation off its subject.
            if distance(old, measured) < 0.08 {
                point = old;
            }
        }
        if self.follows_presence() && self.presence.is_none_or(|(old, _)| old != point) {
            self.gaze_next = self.t;
        }
        let arriving = self.presence.is_none() && self.state == "idle" && self.follows_presence();
        if arriving && self.drives.eagerness() > 0.5 && self.t - self.reunited_at >= REUNION_GAP {
            // Someone sits down after a long time alone: a moment of delight.
            self.reunited_at = self.t;
            self.express("joy", 1.2);
        }
        self.presence = Some((point, self.t + 2.0));
        self.quiet_since = self.t;
    }

    pub fn clear_gaze(&mut self) {
        if self.presence.take().is_some() {
            self.fixation = CENTER;
            self.reset_fixation();
        }
    }

    pub fn glance(&mut self, x: f32, y: f32, hold: f64) {
        if !self.panel_blank && !self.asleep() {
            self.touch = Some(([x.clamp(-1.0, 1.0), y.clamp(-1.0, 1.0)], self.t + hold.clamp(0.1, 2.0)));
            self.quiet_since = self.t;
        }
    }

    /// Returns whether the panel can be shown. A recovery key can show the
    /// face while the lid is unknown, without treating it as a confirmed open.
    pub fn set_lid(&mut self, closed: Option<bool>, show_face: bool, snapshot: bool) -> bool {
        let open = closed == Some(false);
        let was_blank = self.panel_blank;
        self.panel_blank = !(open || (closed.is_none() && show_face));
        if was_blank && !self.panel_blank {
            self.lit_at = self.t;
        }
        if self.panel_blank {
            self.hush_reply();
            self.screen_concern_until = 0.0;
            self.unheard_until = 0.0;
            if !was_blank {
                self.spring = Spring::rebuilt(ExprId::Asleep, self.spring.source_pose);
            }
            self.lid_wake = None;
            self.level = 0.0;
            self.level_target = 0.0;
            self.emotion = None;
            self.blink = None;
            self.blink_repeat = false;
            self.double_blink = false;
            if let Some(cue) = &mut self.battery_cue {
                cue.until = self.t;
            }
            if let Some(cue) = &mut self.heat.cue {
                cue.until = self.t;
            }
            self.touch = None;
            self.presence = None;
            self.fixation = CENTER;
            self.gaze = Gaze::new();
            self.gesture = None;
            self.heaviness = 0.0;
            self.head = 0.0;
            self.head_v = 0.0;
            if let Some(birth) = self.birth.take() {
                // Dark before the ceremony finished: play it again next time.
                if birth.cut.is_none() {
                    self.birth_pending = Some(self.t);
                }
            }
            self.reset_fixation();
        } else if self.birth_pending.is_some_and(|at| self.t - at <= BIRTH_PENDING) {
            self.lit_at = self.t;
            self.start_birth(0.0);
        } else if open && !self.lid_open && !snapshot {
            self.quiet_since = self.t;
            self.lid_wake = Some(LidWake { started: self.t, dreamed: false });
            self.blink = None;
            self.blink_repeat = false;
            self.blink_next = self.t + 2.8;
        }
        self.lid_open = open;
        !self.panel_blank
    }

    pub fn dream_completed_on_open(&mut self) {
        if let Some(wake) = &mut self.lid_wake {
            if wake.started == self.t {
                wake.dreamed = true;
            }
        }
    }

    /// Play an idle gesture now. Returns whether it is one the face knows.
    pub fn perform(&mut self, name: &str) -> bool {
        let gesture = match name {
            "content" => Gesture::Content,
            "yawn" => Gesture::Yawn,
            "nod" => Gesture::Nod,
            "shake" => Gesture::Shake,
            "survey" => {
                if !self.panel_blank && self.state == "idle" && self.presence.is_none() {
                    self.survey();
                }
                return true;
            }
            _ => return false,
        };
        let head_gesture = matches!(gesture, Gesture::Nod | Gesture::Shake);
        if !self.panel_blank && !self.asleep()
            && (!head_gesture || (!self.muted && !self.dazed() && self.lid_wake.is_none() && self.birth.is_none()))
        {
            self.start_gesture(gesture);
        }
        true
    }

    fn start_gesture(&mut self, gesture: Gesture) {
        let side = if self.rng.next() < 0.5 { -1.0 } else { 1.0 };
        self.gesture = Some((gesture, self.t, side));
        // The gesture is its own blink; let a natural one follow it.
        let end = self.t + f64::from(gesture.length());
        self.blink_next = self.blink_next.max(end + f64::from(self.rng.range(0.3, 1.2)));
    }

    /// Turn slowly to look across the room, with a blink on the way.
    fn survey(&mut self) {
        let side = if self.rng.next() < 0.5 { -1.0 } else { 1.0 };
        self.gaze_away = true;
        self.surveying = true;
        self.fixation = [side * self.rng.range(0.72, 0.95), self.rng.range(-0.08, 0.18)];
        self.gaze_next = self.t + f64::from(self.rng.range(1.8, 2.8));
        if self.blink.is_none() && self.gesture.is_none() {
            self.blink_next = self.blink_next.min(self.t + 0.06);
        }
    }

    pub fn panel_blank(&self) -> bool {
        self.panel_blank
    }

    pub fn set_level(&mut self, v: f32) {
        if self.panel_blank {
            return;
        }
        if !v.is_finite() {
            return;
        }
        if self.state == "speaking" && self.level_target > 0.35 && v <= 0.06
            && self.blink.is_none() && self.blink_next - self.t < 0.75
        {
            self.blink_next = self.blink_next.min(self.t + 0.16);
        }
        self.level_target = ((v - 0.06) / 0.94).clamp(0.0, 1.0).sqrt();
        self.level_at = self.t;
        if self.level_target > 0.0 {
            self.quiet_since = self.t;
        }
    }

    pub fn speech_level(&mut self, level: f32, cue: Option<&str>) {
        if self.state == "speaking" && self.speech_ended {
            return;
        }
        self.set_level(level);
        if self.state != "speaking" || self.panel_blank || self.muted || self.dazed()
            || !level.is_finite() || level < 0.015
        {
            return;
        }
        let expression = match cue {
            Some("neutral") => None,
            Some("concern") => Some(ExprId::Concern),
            Some("joy") => Some(ExprId::Joy),
            Some("curiosity") => Some(ExprId::Curiosity),
            Some("surprise") => Some(ExprId::Surprise),
            Some("skepticism") => Some(ExprId::Skepticism),
            Some("sleepy") => Some(ExprId::Sleepy),
            _ => return,
        };
        self.emotion = None;
        self.speech_cue = expression;
    }

    /// React once to a confirmed outcome, in the eye strip as well as full screen.
    pub fn screen_outcome(&mut self, success: bool) {
        if !self.layers_visible() || !self.follows_focus() {
            return;
        }
        if success {
            self.screen_concern_until = 0.0;
            self.glance(0.0, -0.65, 0.50);
            // Replacing a yawn or a contented smile would snap its lids in one frame.
            if !self.gesture.is_some_and(|(g, _, _)| matches!(g, Gesture::Yawn | Gesture::Content)) {
                self.start_gesture(Gesture::Acknowledge);
            }
        } else {
            self.glance(0.0, 0.12, 0.72);
            self.screen_concern_until = self.t + 0.72;
        }
    }

    pub fn set_link_lost(&mut self, lost: bool) {
        if self.link_lost != lost {
            self.link_lost = lost;
            if lost {
                self.hush_reply();
                self.screen_concern_until = 0.0;
                self.unheard_until = 0.0;
                self.set_breath_effort(None);
            }
            self.reset_fixation();
        }
    }

    pub fn asleep(&self) -> bool {
        self.state == "asleep"
    }

    pub fn muted(&self) -> bool {
        self.muted
    }

    fn layers_visible(&self) -> bool {
        !self.panel_blank && !self.muted && !self.dazed() && self.lid_wake.is_none()
            && self.birth.is_none() && !self.loading && self.state != "waking"
    }

    /// Force the Effects layer on or off (tests). In the app the face sees the
    /// layer from the render loop calling `emote_motion`, which it only does
    /// with Effects on. Off (the default) means every live pose is eyes-only:
    /// no mark is produced and none keeps the frame rate up.
    #[cfg(test)]
    pub fn set_effects_layer(&mut self, on: bool) {
        self.effects_layer = on;
    }

    /// How long after the last request for emotes the Effects layer still counts
    /// as on. A few frames, so one skipped frame does not flicker the pupils.
    const EFFECTS_SEEN: f64 = 0.3;

    fn effects_on(&self) -> bool {
        self.effects_layer || self.t - self.effects_called_t.get() < Self::EFFECTS_SEEN
    }

    /// Unwell, link lost or still loading: the honest cues own the face.
    fn pupils_blocked_now(&self) -> bool {
        self.unwell || self.link_lost || self.loading
    }

    fn pupil_guard(&self) -> f32 {
        if self.pupils_blocked_now() || self.t < self.pupil_blocked_at + PUPIL_SETTLE { 0.0 } else { 1.0 }
    }

    /// How much flame and steam may show: nothing while a fresh hot or critical
    /// thermal reading stands (immediately), easing back once it is no longer
    /// fresh. With no reading at all (no Bodycare) they show.
    fn heat_mark_scale(&self) -> f32 {
        if self.t < self.heat.fresh_until {
            0.0
        } else if self.heat.fresh_until <= 0.0 {
            1.0
        } else {
            ease((self.t - self.heat.fresh_until) as f32, 0.0, HEAT_MARK_RETURN)
        }
    }

    /// Live effect channels from the current expression's own authored values,
    /// filtered through the allow-list and the heat guard. `None` when nothing
    /// is showing, the Effects layer is off or a state-true cue owns the face.
    fn live_marks(&self) -> Option<[f32; emote::CHANNELS]> {
        let expression = self.target_expression();
        let speaking = self.state == "speaking" && !self.speech_ended;
        // The sleep bubble is authored on the sleeping poses, so those two pass.
        let sleeping = matches!(expression, ExprId::Asleep | ExprId::Dreaming);
        if !self.effects_on() || !self.layers_visible() || LAB_ONLY.contains(&expression.name())
            || self.battery_cue.is_some() || self.heat.cue.is_some() || self.state == "drowsy"
            || (!decor::allowed(expression) && !speaking && !sleeping)
        {
            return None;
        }
        let age = (self.t - self.acted_at).max(0.0) as f32;
        let release = self.emotion.as_ref().filter(|held| held.id == expression)
            .map_or(1.0, |held| ease((held.until - self.t) as f32, 0.0, 0.18));
        let fade = ease(age, 0.0, 0.15) * release;
        let heat = self.heat_mark_scale();
        let authored = self.authored_effect_values(expression);
        let mut values = [0.0; emote::CHANNELS];
        for (index, value) in values.iter_mut().enumerate() {
            if live_channel(index) {
                let guard = if matches!(emote::NAMES[index], "flame" | "steam") { heat } else { 1.0 };
                *value = authored[index] * fade * guard;
            }
        }
        values.iter().any(|value| *value > 0.012).then_some(values)
    }

    pub fn decor_motion(&self, preview: bool) -> Option<decor::Motion> {
        let expression = self.target_expression();
        let speaking = self.state == "speaking" && !self.speech_ended;
        if !self.layers_visible()
            || (!preview && (self.battery_cue.is_some() || self.heat.cue.is_some() || self.state == "drowsy"
                || (!decor::allowed(expression) && !speaking)))
        {
            return None;
        }
        let strength = self.intensity(expression);
        let age = (self.t - self.acted_at) as f32;
        Some(decor::Motion {
            expression,
            blend: self.blend(expression),
            strength,
            viseme: crate::viseme::Sample::default(),
            pupil_kick: if preview { expression.visual().kick.pupil * strength * pulse(age, 0.0, 0.10, 0.48) } else { 0.0 },
            speech: if self.state == "speaking" { self.level } else { 0.0 },
            time: self.t,
            reflected: self.facing_left(),
            special_pupils: if !preview && self.effects_on() && !LAB_ONLY.contains(&expression.name()) {
                [f32::from(u8::from(live_pupil("cross"))), f32::from(u8::from(live_pupil("spiral")))]
            } else {
                [0.0; 2]
            },
            pupil_guard: if preview || !self.effects_on() { 1.0 } else { self.pupil_guard() },
        })
    }

    pub fn emote_motion(&self, preview: bool) -> Option<([f32; emote::CHANNELS], f32, f64)> {
        let motion = self.emote_motion_now(preview);
        // Asked for after answering, so the layer counts as on from the next
        // frame and turning it on never changes the answer it was seen from.
        if !preview {
            self.effects_called_t.set(self.t);
        }
        motion
    }

    fn emote_motion_now(&self, preview: bool) -> Option<([f32; emote::CHANNELS], f32, f64)> {
        if !self.layers_visible() {
            return None;
        }
        let age = (self.t - self.acted_at).max(0.0);
        if preview {
            let expression = self.target_expression();
            let strength = self.intensity(expression);
            let visual = expression.visual();
            let burst = visual.kick.burst * strength * pulse(age as f32, 0.0, 0.12, 0.95);
            let manual_burst = self.preview_burst.map_or(0.0, |at| pulse((self.t - at) as f32, 0.0, 0.12, 0.95));
            let release = self.emotion.as_ref().filter(|held| held.id == expression)
                .map_or(1.0, |held| ease((held.until - self.t) as f32, 0.0, 0.18));
            let fade = ease(age as f32, 0.0, 0.15) * release;
            let mut effects = self.preview_effect_values(expression);
            for (value, manual) in effects.iter_mut().zip(self.preview_effects) {
                if !manual {
                    *value *= fade;
                }
            }
            return Some((effects, (burst * release).max(manual_burst), age));
        }
        let mut effects = [0.0; emote::CHANNELS];
        if matches!(self.state.as_str(), "attending" | "listening") && self.t < self.unheard_until {
            effects[emote::QUESTION] = 0.68 * ease((self.unheard_until - self.t) as f32, 0.0, 0.25);
        } else if self.state == "thinking" {
            effects[emote::DOTS] = 0.8;
        } else if self.state == "idle" && self.target_expression() == ExprId::Tired
            && self.battery_cue.as_ref().is_some_and(|cue| self.t < cue.until)
        {
            // Only the battery has a glyph; heat shows the tired eyes alone.
            effects[emote::BATTERY] = 0.8;
        } else if let Some(marks) = self.live_marks() {
            effects = marks;
        } else {
            return None;
        }
        Some((effects, 0.0, age))
    }

    /// The eyes are resting: lit, listening for their name, not dazed and not
    /// in a conversation. Space, Return or a click then asks for attention
    /// (`key.attend`), as "Hey Pip" would.
    pub fn resting(&self) -> bool {
        !self.panel_blank && !self.muted && !self.dazed() && matches!(self.state.as_str(), "idle" | "drowsy" | "asleep")
    }

    /// Words were heard but not made out (`hear.missed`): a short squint, the
    /// face's "pardon?". Nothing shows while muted, dazed, asleep or dark.
    pub fn missed(&mut self) {
        if !self.muted && !self.dazed() {
            self.express("skepticism", MISSED_HOLD);
        }
    }

    /// Called after `missed()` for an unclear hearing: the squint becomes
    /// confusion (same suppression: nothing while muted, dazed, asleep or dark).
    pub fn unclear_hearing(&mut self) {
        if !self.muted && !self.dazed() {
            self.express(UNCLEAR_HEARING.0, UNCLEAR_HEARING.1);
        }
        if self.layers_visible() && matches!(self.state.as_str(), "attending" | "listening") {
            self.unheard_until = self.t + MISSED_HOLD;
        }
    }

    fn dazed(&self) -> bool {
        self.unwell || self.link_lost
    }

    /// The expression the face is heading toward right now.
    pub fn target_name(&self) -> &'static str {
        self.target_expression().name()
    }

    fn target_expression(&self) -> ExprId {
        if self.asleep() {
            return if self.preview_dream { ExprId::Dreaming } else { ExprId::Asleep };
        }
        if self.dazed() {
            return if self.loading && !self.unwell { ExprId::Loading } else { ExprId::Dazed };
        }
        if self.muted {
            return ExprId::Muted;
        }
        if let Some((expression, _)) = self.preview_flash.filter(|(_, until)| self.t < *until) {
            return expression;
        }
        if let Some(held) = &self.emotion {
            if self.t < held.until {
                return held.id;
            }
        }
        if self.state == "speaking" {
            return self.speech_cue.unwrap_or(ExprId::Speaking);
        }
        if self.follows_focus() && self.t < self.screen_concern_until {
            return ExprId::Concern;
        }
        if self.state == "idle" && self.tired_cue_showing() {
            return ExprId::Tired;
        }
        match self.state.as_str() {
            "attending" => ExprId::Attentive,
            "listening" => ExprId::Listening,
            "thinking" => ExprId::Thinking,
            "speaking" => ExprId::Speaking,
            "drowsy" => ExprId::Sleepy,
            _ => ExprId::Neutral,
        }
    }

    /// The shape the spring heads for. A yawn keeps the eyes open for its
    /// stretch, and the drowsy shape arrives behind the squeeze.
    fn shape_expression(&self) -> ExprId {
        let expression = self.target_expression();
        let stretching =
            self.gesture.is_some_and(|(g, at, _)| g == Gesture::Yawn && ((self.t - at) as f32) < YAWN_STRETCH);
        if stretching && matches!(expression, ExprId::Sleepy | ExprId::Asleep) {
            ExprId::Neutral
        } else {
            expression
        }
    }

    fn held(&self, id: ExprId) -> Option<&HeldExpression> {
        if self.preview_flash.is_some_and(|(_, until)| self.t < until) {
            return None;
        }
        self.emotion.as_ref().filter(|held| held.id == id && self.t < held.until)
    }

    fn intensity(&self, id: ExprId) -> f32 {
        self.held(id).map_or_else(|| {
            if self.state == "speaking" && self.speech_cue == Some(id) {
                0.55
            } else if id == ExprId::Concern && self.follows_focus() && self.t < self.screen_concern_until {
                0.52
            } else {
                1.0
            }
        }, |held| held.intensity)
    }

    fn blend(&self, id: ExprId) -> Option<(ExprId, f32)> {
        self.held(id).and_then(|held| held.blend)
    }

    fn preview_effect_values(&self, expression: ExprId) -> [f32; emote::CHANNELS] {
        let mut values = self.authored_effect_values(expression);
        for (index, value) in values.iter_mut().enumerate() {
            if self.preview_effects[index] {
                *value = value.max(if index == emote::BREATH { 1.8 } else { 1.0 });
            }
        }
        values
    }

    /// The expression's own effect values at its current strength and blend.
    fn authored_effect_values(&self, expression: ExprId) -> [f32; emote::CHANNELS] {
        let blend = self.blend(expression);
        let neutral = &ExprId::Neutral.visual().effects;
        let intensity = self.intensity(expression);
        let primary = &expression.visual().effects;
        std::array::from_fn(|index| {
            let target = blend.map_or(primary[index], |(other, amount)| {
                mix(primary[index], other.visual().effects[index], amount)
            });
            mix(neutral[index], target, intensity).max(0.0)
        })
    }

    /// The thinking preset leans to one side. A thought off to the other
    /// side reflects it, so the look away reads the same either way.
    fn facing_left(&self) -> bool {
        if self.shape_expression() != ExprId::Thinking {
            return false;
        }
        let pose = ExprId::Thinking.pose();
        (pose.l.x + pose.r.x) * self.thought_side < -0.01
    }

    /// Catch up to the current frame before stamping newly received events.
    /// A long renderer stall must never consume a new touch, expression or wake.
    pub fn frame(&mut self, elapsed: f32, receive: impl FnOnce(&mut Self)) -> Pose {
        let elapsed = elapsed.max(0.0);
        let dt = elapsed.min(0.1);
        let previous = self.t;
        let now = previous + f64::from(elapsed);
        if !self.panel_blank {
            let tempo = match self.state.as_str() {
                "attending" | "listening" => 20.0,
                "drowsy" | "asleep" => 8.0,
                _ => 14.0,
            };
            let speed = if self.state == "waking" { 0.8 } else { 4.5 };
            self.omega += (tempo - self.omega) * (dt * speed).min(1.0);
            let held = self.emotion.as_ref().map_or(elapsed, |held| ((held.until - previous) as f32).clamp(0.0, elapsed));
            self.spring.damping = self.target_expression().damping();
            self.spring.flip = self.facing_left();
            let expression = self.shape_expression();
            self.spring.step(expression, self.intensity(expression), self.blend(expression), held, self.omega);
            if held < elapsed {
                self.emotion = None;
                self.spring.damping = self.target_expression().damping();
                self.spring.flip = self.facing_left();
                let expression = self.shape_expression();
                self.spring.step(expression, self.intensity(expression), self.blend(expression), elapsed - held, self.omega);
            }
            self.step_voice(elapsed);
        }
        self.t = now;
        receive(self);
        self.step_heat();
        if self.pupils_blocked_now() {
            self.pupil_blocked_at = self.t;
        }

        // Notice a new expression arriving, so its entrance can be acted.
        let arriving = self.target_expression();
        let intensity = self.intensity(arriving);
        if arriving != self.acting || intensity != self.acting_intensity {
            let old_interval = self.blink_interval();
            let new_expression = arriving != self.acting;
            self.acted_from = self.acting;
            self.acting = arriving;
            self.acting_intensity = intensity;
            if new_expression {
                self.acted_at = self.t;
            }
            let interval = self.blink_interval();
            if interval != old_interval && !self.blink_repeat && !self.double_blink && self.blink_next > self.t {
                self.blink_next = self.t + (self.blink_next - self.t) * f64::from(interval / old_interval);
            }
            if new_expression && arriving.motion().kick == Kick::Startle && intensity >= 0.55
                && !self.panel_blank && self.lid_wake.is_none()
            {
                // Eyes pop wide, then a quick blink-blink.
                self.double_blink = true;
                if self.blink.is_none() {
                    self.blink_next = self.t + 0.30;
                }
            }
        }

        let mut p = Pose::from_array(&self.spring.x);
        if self.panel_blank {
            p.l.glow = 0.0;
            p.r.glow = 0.0;
            return p;
        }

        let asleep = self.asleep();
        let t = self.t;
        let mut gaze = self.step_gaze();
        let movement = self.preview_gesture.and_then(|(gesture, at)| gesture.motion((t - at) as f32));
        if movement.is_none() {
            self.preview_gesture = None;
        }
        if movement.is_some() || self.preview_gaze != CENTER {
            let target = movement.map_or(CENTER, |movement| movement.gaze);
            let step = 1.0 - (-16.0 * dt).exp();
            for ((offset, axis), target) in self.preview_gaze.iter_mut().zip(&mut gaze).zip(target) {
                *offset = mix(*offset, target, step);
                if target == 0.0 && offset.abs() < 0.001 {
                    *offset = 0.0;
                }
                *axis = (*axis + *offset).clamp(-1.2, 1.2);
            }
        }
        let wake = self
            .lid_wake
            .as_ref()
            .map(|wake| ((self.t - wake.started) as f32, wake.dreamed))
            .filter(|(elapsed, _)| *elapsed < WAKE_DURATION);
        if wake.is_none() {
            self.lid_wake = None;
        }
        let birth = self
            .birth
            .as_ref()
            .map(|b| ((self.t - b.started) as f32, b.cut.map(|at| (self.t - at) as f32)))
            .filter(|&(age, cut)| age < BIRTH && cut.is_none_or(|c| c < BIRTH_HANDOVER));
        if birth.is_none() {
            self.birth = None;
        }
        if wake.is_none() && birth.is_none() {
            self.step_blink();
        }
        if self.lid_open
            && wake.is_none()
            && self.state == "idle"
            && !self.muted
            && !self.dazed()
            && self.emotion.is_none()
        {
            if let Some(cue) = &mut self.battery_cue {
                if self.t >= cue.next {
                    cue.until = self.t + 6.0;
                    cue.next = self.t + 45.0;
                }
            } else if let Some(cue) = &mut self.heat.cue {
                if self.t >= cue.next {
                    cue.until = self.t + HEAT_CUE_ON;
                    cue.next = self.t + HEAT_CUE_EVERY;
                }
            }
        }
        // Carry phase across state changes so a breath never jumps mid-cycle.
        let depth = if asleep { 1.0 } else { 0.0 };
        self.breath_depth += (depth - self.breath_depth) * dt;
        if self.breath_experiment {
            let effort = if asleep { 0.0 } else { self.breath_effort_target };
            self.breath_effort += (effort - self.breath_effort) * dt;
        }
        let rate = f64::from(mix(0.22, 0.13, self.breath_depth) + 0.07 * self.breath_effort);
        self.breath_phase = (self.breath_phase + TAU * rate * f64::from(elapsed)) % TAU;
        let mut breath = self.breath_phase.sin() as f32;
        // Unwell in its sleep: an uneven, unsettled breath.
        let restless = asleep && self.dazed() && !self.loading;
        if restless {
            breath *= 0.65 + 0.35 * (t * 0.37).sin() as f32;
        }

        let expressive = !asleep && !self.muted && !self.dazed();
        let motion = self.acting.motion();
        let concept = self.concept_preview.then(|| self.preview_effect_values(self.acting));
        let strength = if expressive && wake.is_none() && birth.is_none() { self.acting_intensity } else { 0.0 };
        let smooth = 1.0 - (-dt * 9.0).exp();
        let free_gaze = self.touch.is_none() && self.presence.is_none() && !(self.focus.is_some() && self.follows_focus());
        for (i, axis) in gaze.iter_mut().enumerate() {
            let bias = &mut self.gaze_bias[i];
            *bias += (motion.gaze[i] * (if free_gaze { strength } else { 0.0 }) - *bias) * smooth;
            if free_gaze && bias.abs() > 0.0001 {
                *axis = (*axis + *bias).clamp(-1.0, 1.0);
            }
        }
        let breathing = concept.map_or_else(|| mix(1.0, motion.breath, strength), |effects| effects[emote::BREATH].clamp(0.3, 2.5));
        self.breath_scale += (breathing - self.breath_scale) * smooth;
        breath *= self.breath_scale;
        let life = self.step_life(elapsed, dt, expressive, wake.is_none());
        let engaged = self.engaged() && expressive;
        let speech = if expressive { self.level } else { 0.0 };
        let age = (self.t - self.state_at) as f32;
        let drift = [0.006 * (t * 0.081).sin() as f32, 0.004 * (t * 0.107 + 1.0).sin() as f32];
        let wave = |rate: f64, phase: f64| (rate * t + phase).sin() as f32;
        let shiver = concept.map_or(0.0, |effects| {
            effects[emote::SHIVER] * ease((t - self.acted_at) as f32, 0.0, 0.14)
        });
        for (side, e) in [(-1.0f32, &mut p.l), (1.0f32, &mut p.r)] {
            e.h *= 1.0 + mix(0.011, 0.045, self.breath_depth) * breath;
            e.y += mix(0.003, 0.012, self.breath_depth) * breath;
            e.x += gaze[0] * 0.137 + drift[0];
            e.y += gaze[1] * 0.083 + drift[1];
            if shiver > 0.0 {
                e.x += 0.010 * shiver * wave(39.0, 0.0);
                e.y += 0.007 * shiver * wave(47.0, 1.1);
            }
            let near = 1.0 + 0.045 * side * gaze[0];
            e.w *= near;
            e.h *= near;
            e.tilt += 0.038 * gaze[0];

            if engaged {
                let focus_age = age - (side + 1.0) * 0.012;
                let focus = ease(focus_age, 0.0, 0.10) * (1.0 - ease(focus_age, 0.12, 0.38));
                e.h *= 1.0 + 0.048 * focus;
                e.y += 0.013 * focus;
            }
            let accent = speech * (1.0 - 0.11 * side);
            e.h *= 1.0 + 0.086 * accent;
            e.y += 0.012 * accent;
            e.glow *= 1.0 + 0.105 * speech;
            let roll = movement.map_or(0.0, |movement| movement.roll);
            life.apply(e, side, self.heaviness, self.head + roll);
            if let Some(movement) = movement {
                e.x += movement.x;
                e.y += movement.y;
            }

            if !asleep && self.dazed() {
                if self.loading && !self.unwell {
                    // Waiting for the body: a slow, even glow, both eyes together.
                    e.glow *= 0.88 + 0.12 * wave(1.6, 0.0);
                } else {
                    e.tilt += 0.038 * wave(1.65, f64::from(side));
                    e.glow *= 0.94 + 0.055 * wave(2.2, 2.0 * f64::from(side));
                }
            }
            if restless {
                // A dazed sleep: lids a little uneven, the face slightly askew.
                e.tilt += 0.05 * side + 0.02 * wave(0.29, f64::from(side));
                e.y += 0.010 * side;
                e.top += 0.05 * (side + 1.0) * 0.5;
                e.glow *= 0.92 + 0.06 * wave(0.53, 1.0 + f64::from(side));
            }

            if self.preview_dream {
                // A brief movement behind the sleeping lids, then stillness.
                let age = age - (side + 1.0) * 0.055;
                let flutter = ease(age, 0.42, 0.62) * (1.0 - ease(age, 0.64, 1.05));
                dream_flutter(e, flutter);
            }

            e.h = e.h.max(0.012);
            if let Some(blink) = &self.blink {
                close_lids(e, blink.closure(self.t, side));
            }
            if self.spring.guarded || self.concept_preview {
                // Audit poses may take larger paths between lids, gaze and
                // roll. Keep the oriented eye inside the existing safe area.
                e.w = e.w.clamp(0.012, 1.45);
                e.h = e.h.clamp(0.012, 0.85);
                e.round = e.round.max(if self.spring.source_pose { 1.5 } else { 2.0 });
                e.glow = e.glow.clamp(0.0, 1.3);
                let (sin, cos) = e.tilt.sin_cos();
                let bulge = (2.0_f32.powf(0.5 - 1.0 / e.round) - 1.0) * e.w.min(e.h);
                let reach_x = (e.w * cos).hypot(e.h * sin) + bulge;
                let reach_y = (e.w * sin).hypot(e.h * cos) + bulge;
                let edge_x = (1.499 - e.w.max(reach_x)).max(0.0);
                let edge_y = (0.899 - e.h.max(reach_y)).max(0.0);
                e.x = e.x.clamp(-edge_x, edge_x);
                e.y = e.y.clamp(-edge_y, edge_y);
            }
        }
        let p = wake.map_or(p, |(elapsed, dreamed)| lid_wake_pose(elapsed, p, dreamed));
        match birth {
            Some((age, None)) => birth_pose(age, p),
            Some((age, Some(cut))) => blend_pose(birth_pose(age, p), p, ease(cut, 0.0, BIRTH_HANDOVER)),
            None => p,
        }
    }

    /// Everything that makes it seem alive between events: the acting of an
    /// arriving expression, idle gestures, heavy lids and the head's roll.
    fn step_life(&mut self, elapsed: f32, dt: f32, expressive: bool, awake: bool) -> Life {
        if self.gesture.is_some_and(|(g, at, _)| (self.t - at) as f32 >= g.length()) {
            self.gesture = None;
        }
        let composed = !self.muted && !self.dazed();
        if !composed {
            self.gesture = None;
        }

        // Lids grow heavy over a long quiet spell alone, and lift at once
        // for company. A yawn marks the first real heaviness.
        let alone = self.state == "idle" && expressive && awake && self.emotion.is_none();
        let heavy = if alone && self.presence.is_none() && self.touch.is_none() {
            let quiet = 0.42 * ease((self.t - self.quiet_since) as f32, 150.0, 480.0);
            (quiet + self.drives.tiredness()).min(0.42)
        } else {
            0.0
        };
        let time_constant = if heavy > self.heaviness { 4.0 } else { 0.35 };
        self.heaviness += (heavy - self.heaviness) * (1.0 - (-elapsed / time_constant).exp());
        if heavy == 0.0 && self.heaviness < 0.05 {
            self.yawned = false;
        }
        if alone && self.gesture.is_none() && self.blink.is_none() && !self.typing_hidden && self.focus.is_none() {
            if self.heaviness > 0.3 && !self.yawned {
                self.yawned = true;
                self.start_gesture(Gesture::Yawn);
            } else if self.t >= self.gesture_next {
                self.gesture_next = self.t + f64::from(self.rng.range(20.0, 38.0));
                if self.heaviness < 0.2 && !self.tired_cue_showing() {
                    self.start_gesture(Gesture::Content);
                }
            }
        }

        let (kind, gesture_age, gesture_side) =
            self.gesture.map_or((None, 0.0, 0.0), |(g, at, side)| (Some(g), (self.t - at) as f32, side));
        let content = if kind == Some(Gesture::Content) {
            ease(gesture_age, 0.0, 0.45) * (1.0 - ease(gesture_age, 1.35, 2.2))
        } else {
            0.0
        };
        let (inhale, squeeze) = if kind == Some(Gesture::Yawn) {
            (
                ease(gesture_age, 0.0, 0.45) * (1.0 - ease(gesture_age, 0.55, 0.85)),
                ease(gesture_age, 0.6, 0.95) * (1.0 - ease(gesture_age, 1.5, 2.3)),
            )
        } else {
            (0.0, 0.0)
        };

        // The head rolls a little with curiosity, listening and thought.
        let mut roll = if expressive {
            match self.acting {
                ExprId::Curiosity => -0.075 * self.acting_intensity,
                ExprId::Listening => 0.028 * self.listen_side * self.acting_intensity,
                ExprId::Thinking => 0.04 * self.thought_side * self.acting_intensity,
                _ => self.acting.motion().roll * self.acting_intensity,
            }
        } else {
            0.0
        };
        let age = (self.t - self.acted_at) as f32;
        let concept = (self.concept_preview && expressive && awake).then(|| self.preview_effect_values(self.acting));
        let visual_kick = self.acting.visual().kick;
        if let Some(effects) = concept {
            roll += 0.12 * effects[emote::SWAY] * (2.6 * self.t).sin() as f32
                + self.acting_intensity * visual_kick.roll * pulse(age, 0.02, 0.14, 0.56);
        }
        roll += gesture_side * (0.035 * content + 0.05 * squeeze);
        let steps = (dt / 0.02).ceil().max(1.0);
        let h = dt / steps;
        for _ in 0..steps as usize {
            self.head_v += (70.0 * (roll - self.head) - 13.4 * self.head_v) * h;
            self.head += self.head_v * h;
        }

        let kick = self.acting.motion().kick;
        let strength = if expressive { self.acting_intensity } else { 0.0 };
        let tuck = if expressive && self.acting.lively() {
            anticipation(age) * (if kick == Kick::Startle { 1.6 } else { 1.0 }) * strength
        } else {
            0.0
        };
        let mut bounce = match self.acting {
            _ if !expressive => 0.0,
            ExprId::Joy => 0.042 * hop(age - 0.07, 0.24, 2.0),
            ExprId::Attentive if matches!(self.acted_from, ExprId::Neutral | ExprId::Sleepy | ExprId::Tired) => {
                0.036 * (1.0 + 1.2 * self.reunion) * hop(age - 0.04, 0.26, 1.0)
            }
            _ if kick == Kick::Laugh => 0.055 * hop(age - 0.07, 0.215, 2.0),
            _ if kick == Kick::Relief => -0.026 * pulse(age, 0.12, 0.48, 0.86),
            _ => 0.0,
        } * strength;
        let mut pop = if kick == Kick::Startle {
            ease(age, 0.04, 0.11) * (1.0 - ease(age, 0.13, 0.45))
        } else {
            0.0
        } * strength;
        let mut recoil = match kick {
            Kick::Startle => 0.9 * pulse(age, 0.14, 0.24, 0.55),
            Kick::Flinch => 0.72 * pulse(age, 0.03, 0.15, 0.48),
            _ => 0.0,
        } * strength;
        if let Some(effects) = concept {
            let envelope = ease(age, 0.0, 0.18);
            bounce += 0.04 * effects[emote::BOUNCE] * envelope * (9.0 * age).sin().abs();
            if self.acting.is_concept_only() && kick == Kick::None {
                pop += 1.5 * visual_kick.pop * pulse(age, 0.0, 0.08, 0.32) * strength;
                recoil += 13.0 * visual_kick.recoil * pulse(age, 0.05, 0.16, 0.59) * strength;
            }
        }
        let nod = if expressive && kind == Some(Gesture::Nod) {
            0.024 * pulse(gesture_age, 0.0, 0.10, 0.23)
                - 0.089 * pulse(gesture_age, 0.10, 0.32, 0.68)
        } else if expressive && kind == Some(Gesture::Acknowledge) {
            0.020 * pulse(gesture_age, 0.0, 0.10, 0.20)
                - 0.024 * pulse(gesture_age, 0.14, 0.27, 0.55)
        } else {
            0.0
        };
        let shake = if expressive && kind == Some(Gesture::Shake) {
            gesture_side * (pulse(gesture_age, 0.0, 0.18, 0.38)
                - 0.95 * pulse(gesture_age, 0.18, 0.46, 0.72)
                + 0.32 * pulse(gesture_age, 0.46, 0.68, 0.82))
        } else {
            0.0
        };
        let smile = (content > 0.0).then(|| ExprId::Joy.pose());
        Life { tuck, bounce, pop, recoil, nod, shake, content, inhale, squeeze, smile }
    }

    fn step_voice(&mut self, elapsed: f32) {
        // A stale sample can only drive the envelope for its remaining lifetime.
        let audible = self.state == "speaking" && !self.muted && !self.dazed() && self.level_target > 0.0;
        let voiced = if audible { ((self.level_at + 0.20 - self.t) as f32).clamp(0.0, elapsed) } else { 0.0 };
        if voiced > 0.0 && self.level != self.level_target {
            let time_constant = if self.level_target > self.level { 0.044 } else { 0.15 };
            self.level = mix(self.level, self.level_target, 1.0 - (-voiced / time_constant).exp());
        }
        if self.level > 0.0 && elapsed > voiced {
            self.level *= (-(elapsed - voiced) / 0.15).exp();
            // Levels this low are visually subpixel. Stop integrating in silence.
            if self.level < 0.0005 {
                self.level = 0.0;
            }
        }
    }

    pub fn set_idle_glances(&mut self, enabled: bool) {
        if self.idle_glances != enabled {
            self.idle_glances = enabled;
            if self.state == "idle" && self.presence.is_none() {
                self.gaze_next = self.t;
            }
        }
    }

    fn step_gaze(&mut self) -> [f32; 2] {
        if self.presence.is_some_and(|(_, until)| self.t >= until) {
            self.presence = None;
            if self.follows_presence() {
                self.reset_fixation();
            }
        }

        if let Some(focus) = self.focus.filter(|_| self.follows_focus()) {
            return self.step_focus(focus);
        }

        if let Some(end) = self.speech_end {
            let age = self.t - end;
            if age >= 2.0 || self.state != "thinking" || self.muted || self.dazed() {
                self.speech_end = None;
                self.gaze_next = self.t;
            } else if self.touch.is_none() {
                let subject = self.presence.map_or(CENTER, |(point, _)| point);
                let [x, y] = self.thought_spot;
                self.gaze_away = age >= 0.30;
                self.fixation = if !self.gaze_away {
                    subject
                } else if age < 1.0 {
                    [x, y]
                } else {
                    match ((age - 1.0) / 0.25) as u32 {
                        0 => [x + 0.08 * self.thought_side, y + 0.06],
                        1 => [x - 0.06 * self.thought_side, y],
                        2 => [x + 0.07 * self.thought_side, y - 0.07],
                        _ => [x, y],
                    }
                };
                return self.gaze.track(self.fixation, self.t, 0.0);
            }
        }

        if self.t >= self.gaze_next {
            let subject = self.presence.map_or(CENTER, |(point, _)| point);
            let returning = self.surveying && self.gaze_away;
            self.surveying = false;
            let (point, wait) = if self.asleep() || self.muted || self.dazed() || self.state == "waking" {
                (CENTER, 1.0)
            } else if self.engaged() || (self.state == "idle" && self.presence.is_some()) {
                self.gaze_away = false;
                let correction = [self.rng.range(-0.027, 0.027), self.rng.range(-0.018, 0.018)];
                let wait = if self.state == "listening" {
                    self.rng.range(2.2, 3.8)
                } else {
                    self.rng.range(0.9, 2.0)
                };
                ([subject[0] + correction[0], subject[1] + correction[1]], wait)
            } else if self.state == "drowsy" {
                ([0.0, -0.22], 3.0)
            } else if self.state == "thinking" {
                self.gaze_away = !self.gaze_away;
                if self.gaze_away {
                    let x = self.thought_side * self.rng.range(0.50, 0.68);
                    self.thought_spot = [x, self.rng.range(0.38, 0.56)];
                    self.search_next = self.t + f64::from(self.rng.range(0.55, 1.0));
                    (self.thought_spot, self.rng.range(3.8, 6.4))
                } else {
                    // Briefly check back with the listener during a long pause.
                    ([0.65 * subject[0], 0.65 * subject[1]], self.rng.range(0.55, 0.90))
                }
            } else if self.state == "idle" && !self.idle_glances {
                self.gaze_away = false;
                (CENTER, 2.0)
            } else if self.gaze_away {
                self.gaze_away = false;
                // Come back from a look around the room as unhurriedly.
                self.surveying = returning;
                (CENTER, self.rng.range(2.5, 5.3))
            } else if self.state == "idle" && !self.typing_hidden && self.rng.next() < 0.28 {
                self.survey();
                (self.fixation, (self.gaze_next - self.t) as f32)
            } else {
                self.gaze_away = true;
                let side = if self.rng.next() < 0.5 { -1.0 } else { 1.0 };
                ([side * self.rng.range(0.24, 0.51), self.rng.range(-0.22, 0.29)], self.rng.range(0.75, 1.55))
            };
            self.fixation = point.map(|x| x.clamp(-1.0, 1.0));
            // Idle looks linger after a long quiet and quicken after news.
            let wandering = self.state == "idle" && self.presence.is_none() && !self.surveying;
            let tempo = if wandering { self.drives.gaze_tempo() } else { 1.0 };
            self.gaze_next = self.t + f64::from(wait * tempo);
        } else if self.state == "thinking"
            && self.gaze_away
            && !self.muted
            && !self.dazed()
            && self.t >= self.search_next
            && self.t + 0.35 < self.gaze_next
        {
            // Small searching hops around the spot it is thinking at.
            // Big enough to read on the laptop panel, small enough to stay calm.
            let reach = self.thought_side * self.rng.range(-0.07, 0.14);
            let spot = self.thought_spot;
            self.fixation = [(spot[0] + reach).clamp(-1.0, 1.0), (spot[1] + self.rng.range(-0.07, 0.07)).clamp(-1.0, 1.0)];
            self.search_next = self.t + f64::from(self.rng.range(0.4, 0.9));
        }

        if self.touch.is_some_and(|(_, until)| self.t >= until) {
            self.touch = None;
        }
        let unhurried = if self.surveying && self.touch.is_none() { 0.62 } else { 0.0 };
        self.gaze.track(self.touch.map_or(self.fixation, |(point, _)| point), self.t, unhurried)
    }

    /// The panel's focus wins over camera presence and idle looks, but not
    /// over someone talking to Pip, sleep or illness.
    fn follows_focus(&self) -> bool {
        !self.muted && !self.dazed() && !self.asleep() && matches!(self.state.as_str(), "idle" | "thinking")
    }

    fn step_focus(&mut self, focus: Focus) -> [f32; 2] {
        self.surveying = false;
        self.gaze_away = false;
        self.gaze_next = self.t + 0.5;
        match focus.kind {
            FocusKind::Paused => self.fixation = [0.0, 0.12],
            FocusKind::Typing | FocusKind::Watching => {
                let target = focus.at.map(|v| v.clamp(-1.0, 1.0));
                if distance(target, self.focus_seen) > 0.015 {
                    // A new line after Enter: the cursor drops and swings back left.
                    let enter = target[1] < self.focus_seen[1] - 0.004 && target[0] < self.focus_seen[0] - 0.3;
                    self.focus_seen = target;
                    // Reading saccades land 80-160 ms after the cursor jumps.
                    self.focus_next = self.t + f64::from(self.rng.range(0.08, 0.16));
                    if enter && focus.kind == FocusKind::Typing && self.blink.is_none() {
                        self.blink_next = self.blink_next.min(self.t + 0.12);
                    }
                }
                if self.t >= self.focus_next {
                    self.fixation = self.focus_seen;
                }
            }
        }
        if self.touch.is_some_and(|(_, until)| self.t >= until) {
            self.touch = None;
        }
        self.gaze.track(self.touch.map_or(self.fixation, |(point, _)| point), self.t, 0.0)
    }

    fn blink_interval(&self) -> f32 {
        mix(1.0, self.acting.motion().blink_interval, self.acting_intensity)
    }

    fn step_blink(&mut self) {
        if self.preview_blink_manual {
            if self.blink.as_ref().is_some_and(|blink| blink.done(self.t)) {
                self.blink = None;
            }
            return;
        }
        if self.asleep() {
            if self.blink.as_ref().is_some_and(|blink| blink.done(self.t)) {
                self.blink = None;
            }
            self.blink_next = self.t + 2.0;
            return;
        }
        let drowsy = matches!(self.target_expression(), ExprId::Sleepy | ExprId::Tired) || self.heaviness > 0.22;
        if let Some(blink) = &self.blink {
            if !blink.done(self.t) {
                return;
            }
            self.blink_repeat = blink.repeat;
            self.blink = None;
            let wait = if self.double_blink {
                ((self.acted_at + 0.30 - self.t) as f32).max(0.05)
            } else if self.blink_repeat {
                self.rng.range(0.14, 0.23)
            } else if self.state == "listening" {
                self.rng.range(3.8, 6.8)
            } else if self.focus.is_some_and(|f| f.kind == FocusKind::Typing) {
                // Reading what it types: blinks come further apart.
                self.rng.range(4.0, 8.5)
            } else {
                self.rng.range(2.5, 5.6)
            };
            let interval = if self.blink_repeat || self.double_blink { 1.0 } else { self.blink_interval() };
            self.blink_next = self.t + f64::from(wait * interval);
        }
        if self.t < self.blink_next {
            return;
        }
        // Let an upcoming blink land in a gap in speech when one is near.
        // Still blink during sustained speech, and keep a double blink intact.
        let double = self.double_blink;
        if self.state == "speaking" && self.level > 0.32 && self.t < self.blink_next + 0.42 && !self.blink_repeat && !double
        {
            return;
        }
        self.double_blink = false;
        let secondary = self.blink_repeat;
        let startle = double || (secondary && self.startle_followup);
        self.startle_followup = double;
        let soft = !drowsy && !startle && self.rng.next() < 0.22;
        let depth = if soft { self.rng.range(0.55, 0.78) } else { 1.0 };
        let pace = if startle {
            0.8
        } else if drowsy {
            1.55
        } else {
            self.rng.range(0.87, 1.17)
        };
        let stagger = self.rng.range(0.007, 0.018);
        self.blink = Some(Blink {
            start: self.t,
            close: 0.075 * pace,
            hold: 0.035 * pace,
            open: 0.120 * pace,
            stagger: if self.rng.next() < 0.5 { stagger } else { -stagger },
            depth,
            repeat: double || (!secondary && !soft && !drowsy && self.state != "listening" && self.rng.next() < 0.12),
        });
        self.blink_repeat = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(a: &mut Anim, secs: f32) -> Pose {
        let mut p = a.frame(0.0, |_| {});
        for _ in 0..(secs * 60.0) as usize {
            p = a.frame(1.0 / 60.0, |_| {});
        }
        p
    }

    fn aperture(e: Eye, x: f32) -> f32 {
        let bow = 1.0 - x * x;
        let ellipse = bow.sqrt();
        let top = e.top + e.top_slant * x + e.top_curve * bow;
        let bot = e.bot + e.bot_slant * x + e.bot_curve * bow;
        (top.min(ellipse) - bot.max(-ellipse)).max(0.0) * e.h
    }

    #[test]
    fn spring_is_consistent_at_twenty_and_sixty_hz() {
        let mut fast = Spring::new(ExprId::Neutral);
        let mut slow = Spring::new(ExprId::Neutral);
        let mut hitch = Spring::new(ExprId::Neutral);
        for _ in 0..18 {
            fast.step(ExprId::Joy, 1.0, None, 1.0 / 60.0, 14.0);
        }
        for _ in 0..6 {
            slow.step(ExprId::Joy, 1.0, None, 1.0 / 20.0, 14.0);
        }
        hitch.step(ExprId::Joy, 1.0, None, 0.3, 14.0);
        for (i, x) in fast.x.into_iter().enumerate() {
            assert!((x - slow.x[i]).abs() < 0.00002);
            assert!((x - hitch.x[i]).abs() < 0.00002);
        }
    }

    #[test]
    fn wakes_from_closed_eyes() {
        let mut a = Anim::new(7);
        let first = a.frame(0.0, |_| {});
        assert!(first.l.glow < 0.5, "starts dim and closed");
        a.set_state("idle", false, false);
        let later = run(&mut a, 6.0);
        assert!(later.l.glow > 0.85, "eyes are lit once awake");
        assert!(later.l.h > 0.2, "eyes are open once awake");
    }

    #[test]
    fn lid_open_plays_one_gesture_in_spite_of_model_state() {
        let mut a = Anim::new(7);
        a.set_state("asleep", true, true);
        assert!(!a.set_lid(Some(true), false, false));
        assert_eq!(a.frame(0.0, |_| {}).l.glow, 0.0);

        assert!(a.set_lid(Some(false), false, false));
        let first = a.frame(0.0, |_| {});
        assert!(first.l.h < 0.03 && first.l.glow > 0.0);
        let opening = run(&mut a, 0.26);
        assert!(opening.l.h > opening.r.h + 0.03);

        a.set_state("idle", true, false);
        a.set_lid(Some(false), false, false); // duplicate open
        let squint = run(&mut a, 0.29);
        assert!(squint.l.h > 0.18 && squint.r.h > 0.18);
        let blink = run(&mut a, 0.19);
        assert!(aperture(blink.l, 0.0) < 0.025 && aperture(blink.r, 0.0) < 0.025);
        assert!(run(&mut a, 0.17).l.h > 0.22);
        assert!(run(&mut a, 0.65).l.h < 0.09); // actual muted pose afterward

        a.set_lid(Some(false), false, true); // retained open on reconnect
        assert!(a.lid_wake.is_none());
    }

    #[test]
    fn appliance_start_waits_for_a_visible_lid_snapshot() {
        let mut a = Anim::new(7);
        assert!(!a.set_lid(None, false, true));
        a.set_state("idle", false, false);
        assert_eq!(run(&mut a, 0.4).l.glow, 0.0);
        assert!(a.set_lid(Some(false), false, true));
        assert!(a.frame(0.0, |_| {}).l.glow > 0.0);
        assert!(a.lid_wake.is_none());
    }

    #[test]
    fn low_battery_waits_for_idle_and_clears_on_ac() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, true);
        a.set_state("speaking", false, false);
        a.express("joy", 3.0);
        a.frame(0.1, |a| a.set_low_battery(true, None));
        assert_eq!(a.target_name(), "joy");
        a.frame(0.1, |a| a.set_state("idle", false, false));
        assert_eq!(a.target_name(), "tired");

        a.frame(0.1, |a| a.set_state("idle", true, false));
        assert_eq!(a.target_name(), "muted");
        a.frame(0.1, |a| a.set_state("idle", true, true));
        assert_eq!(a.target_name(), "dazed");
        a.frame(0.1, |a| {
            a.set_low_battery(false, None);
            a.set_state("idle", false, false);
        });
        assert_eq!(a.target_name(), "neutral");
    }

    #[test]
    fn battery_cue_uses_six_second_window_and_ignores_repeated_readings() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        a.frame(0.0, |a| a.set_low_battery(true, None));
        assert_eq!(a.target_name(), "tired");
        a.frame(5.0, |a| a.set_low_battery(true, None));
        assert_eq!(a.target_name(), "tired");
        a.frame(1.1, |a| a.set_low_battery(true, None));
        assert_eq!(a.target_name(), "neutral");
        a.frame(39.0, |a| a.set_low_battery(true, None));
        assert_eq!(a.target_name(), "tired");
        a.frame(0.1, |a| {
            a.set_lid(Some(true), false, false);
            a.set_state("asleep", false, false);
        });
        let blank = a.frame(0.0, |_| {});
        assert_eq!((blank.l.glow, blank.r.glow), (0.0, 0.0));
    }

    #[test]
    fn leaving_low_battery_on_the_charger_is_a_relief_and_nothing_else_is() {
        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(true, Some(false)));
        a.frame(0.1, |a| a.set_low_battery(false, Some(true)));
        assert_eq!(a.target_name(), "relieved");
        run(&mut a, CHARGER_RELIEF.1 as f32 + 0.5);
        assert_ne!(a.target_name(), "relieved");

        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(false, Some(true)));
        assert_ne!(a.target_name(), "relieved", "never low, nothing to relieve");

        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(true, Some(false)));
        a.frame(0.1, |a| a.set_low_battery(false, Some(false)));
        assert_ne!(a.target_name(), "relieved", "the charger is not in");
        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(true, Some(false)));
        a.frame(0.1, |a| a.set_low_battery(false, None));
        assert_ne!(a.target_name(), "relieved", "an unknown charger proves nothing");

        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(true, None));
        a.set_state("asleep", false, false);
        a.frame(0.1, |a| a.set_low_battery(false, Some(true)));
        assert_eq!(a.target_name(), "asleep");
        // Muted and unwell outrank the relief, as they do every expression.
        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(true, None));
        a.set_state("idle", true, false);
        a.frame(0.1, |a| a.set_low_battery(false, Some(true)));
        assert_eq!(a.target_name(), "muted");
        let mut a = awake(3);
        a.frame(0.1, |a| a.set_low_battery(true, None));
        a.set_state("idle", false, true);
        a.frame(0.1, |a| a.set_low_battery(false, Some(true)));
        assert_eq!(a.target_name(), "dazed");
    }

    fn hot(a: &mut Anim) {
        a.thermal_budget(Some("hot"), Some("thermal"), true, 12_000.0);
    }

    /// Step half-second frames, feeding `feed` each frame, and list the times
    /// at which the face looked tired.
    fn tired_times(a: &mut Anim, secs: f64, mut feed: impl FnMut(&mut Anim)) -> Vec<f64> {
        let mut seen = Vec::new();
        for _ in 0..(secs * 2.0) as usize {
            a.frame(0.5, |a| feed(a));
            if a.target_name() == "tired" {
                seen.push(a.t);
            }
        }
        seen
    }

    #[test]
    fn heat_shows_tired_only_after_thirty_seconds_of_fresh_hot_readings() {
        let mut a = awake(5);
        let start = a.t;
        let mut n = 0;
        let seen = tired_times(&mut a, 40.0, |a| {
            n += 1;
            if n % 4 == 0 {
                hot(a);
            }
        });
        assert!(!seen.is_empty());
        let first = seen[0] - start;
        assert!((29.0..=32.0).contains(&first), "first at {first}");
        // Six seconds on, then off until the 45 s repeat.
        assert!(seen.last().unwrap() - seen[0] <= HEAT_CUE_ON + 0.6, "{seen:?}");
        let again = tired_times(&mut a, 50.0, |a| hot(a));
        assert!(again.first().is_some_and(|t| (t - seen[0] - HEAT_CUE_EVERY).abs() < 1.5), "{again:?}");
    }

    #[test]
    fn a_late_republish_keeps_the_run_but_a_long_gap_restarts_it() {
        // Bodycare's real cadence is about 10 s against a 12 s ttl, and load
        // makes it late: readings 14 s apart (2 s past the ttl) keep the run.
        let mut a = awake(5);
        let mut n = 0;
        let seen = tired_times(&mut a, 48.0, |a| {
            n += 1;
            if n % 28 == 1 {
                hot(a);
            }
        });
        assert!(!seen.is_empty(), "late readings must still arm the cue");
        // Readings 20 s apart (8 s past the ttl) never make 30 s in a row.
        let mut b = awake(5);
        let mut m = 0;
        let seen = tired_times(&mut b, 120.0, |b| {
            m += 1;
            if m % 40 == 1 {
                hot(b);
            }
        });
        assert!(seen.is_empty(), "{seen:?}");
    }

    #[test]
    fn a_revoke_inside_the_slack_still_breaks_the_run() {
        let mut a = awake(5);
        assert!(tired_times(&mut a, 20.0, |a| hot(a)).is_empty());
        // A revoke right after a lapse is a break, and 20 s more is not 30.
        a.frame(0.0, |a| a.thermal_budget(None, None, false, 0.0));
        assert!(tired_times(&mut a, 20.0, |a| hot(a)).is_empty());
    }

    #[test]
    fn critical_pressure_counts_as_heat() {
        let mut a = awake(5);
        let seen = tired_times(&mut a, 35.0, |a| a.thermal_budget(Some("critical"), Some("thermal"), true, 5000.0));
        assert!(!seen.is_empty());
    }

    #[test]
    fn one_hot_readout_that_lapses_before_thirty_seconds_never_starts_the_cue() {
        let mut a = awake(5);
        a.frame(0.0, |a| a.thermal_budget(Some("hot"), Some("thermal"), true, 12_000.0));
        // The ttl is 12 s and nothing refreshes it: no cue, even much later.
        let seen = tired_times(&mut a, 120.0, |_| {});
        assert!(seen.is_empty(), "{seen:?}");
        assert!(a.heat.cue.is_none());
        // A ttl over the cap is held to 12 s as well.
        a.frame(0.0, |a| a.thermal_budget(Some("hot"), Some("thermal"), true, 600_000.0));
        assert!(tired_times(&mut a, 60.0, |_| {}).is_empty());
    }

    #[test]
    fn a_gap_in_fresh_readings_restarts_the_sustain_clock() {
        let mut a = awake(5);
        // 20 s hot, a cooler reading, then 20 s hot: never 30 s in a row.
        assert!(tired_times(&mut a, 20.0, |a| hot(a)).is_empty());
        a.frame(0.0, |a| a.thermal_budget(Some("warm"), Some("thermal"), true, 12_000.0));
        assert!(tired_times(&mut a, 20.0, |a| hot(a)).is_empty());
        // Ten more seconds finish a run of 30 from the cooler reading.
        assert!(!tired_times(&mut a, 12.0, |a| hot(a)).is_empty());
    }

    #[test]
    fn another_priority_is_not_heat() {
        let mut a = awake(5);
        for priority in ["power_source", "lid", "cognitive", "power_unknown"] {
            let seen = tired_times(&mut a, 40.0, |a| a.thermal_budget(Some("hot"), Some(priority), true, 12_000.0));
            assert!(seen.is_empty(), "{priority}");
        }
        assert!(tired_times(&mut a, 40.0, |a| a.thermal_budget(Some("hot"), None, true, 12_000.0)).is_empty());
    }

    #[test]
    fn a_revoke_in_the_middle_of_a_running_cue_clears_it_on_the_sixty_second_rule() {
        let mut a = awake(5);
        assert!(!tired_times(&mut a, 32.0, |a| hot(a)).is_empty());
        assert!(a.heat.cue.is_some());
        // State epoch change: the brainstem revokes the budget.
        a.frame(0.0, |a| a.thermal_budget(None, None, false, 0.0));
        // The cue is not cut short: its next repeat still comes inside 60 s.
        let during = tired_times(&mut a, 55.0, |_| {});
        assert!(!during.is_empty(), "the armed cue keeps its schedule");
        // Past 60 s without a hot reading it is gone and stays gone.
        let after = tired_times(&mut a, 10.0, |_| {});
        assert!(a.heat.cue.is_none());
        assert!(tired_times(&mut a, 120.0, |_| {}).is_empty());
        assert!(after.iter().all(|t| *t < a.t));
    }

    #[test]
    fn a_hot_reading_during_the_clearing_minute_keeps_the_cue_running() {
        let mut a = awake(5);
        tired_times(&mut a, 32.0, |a| hot(a));
        a.frame(0.0, |a| a.thermal_budget(Some("nominal"), Some("thermal"), true, 12_000.0));
        tired_times(&mut a, 40.0, |_| {});
        assert!(a.heat.cue.is_some());
        hot(&mut a);
        tired_times(&mut a, 40.0, |_| {});
        assert!(a.heat.cue.is_some(), "the minute starts again once hot readings stop");
    }

    #[test]
    fn the_heat_cue_waits_for_idle_and_never_doubles_the_battery_cue() {
        let mut a = awake(5);
        a.set_state("thinking", false, false);
        assert!(tired_times(&mut a, 40.0, |a| hot(a)).is_empty());
        assert!(a.heat.cue.is_some(), "armed, but the face is busy");
        a.frame(0.1, |a| a.set_state("idle", false, false));
        assert!(!tired_times(&mut a, 3.0, |a| hot(a)).is_empty());
        // A low battery takes the schedule: heat stays silent while it is flagged.
        let mut a = awake(5);
        tired_times(&mut a, 32.0, |a| hot(a));
        a.frame(0.0, |a| a.set_low_battery(true, Some(false)));
        a.frame(7.0, |a| hot(a));
        let battery_until = a.battery_cue.as_ref().unwrap().until;
        let heat_until = a.heat.cue.as_ref().unwrap().until;
        assert!(battery_until > heat_until || a.t >= heat_until);
        // No readings at all keep the factory face untouched.
        let mut a = awake(5);
        assert!(tired_times(&mut a, 120.0, |_| {}).is_empty());
        assert!(a.heat.cue.is_none());
    }

    #[test]
    fn an_unclear_hearing_looks_confused_and_a_silent_one_stays_sceptical() {
        let mut a = awake(1);
        a.set_state("attending", false, false);
        a.missed();
        a.unclear_hearing();
        assert_eq!(a.target_name(), "confused");
        run(&mut a, (UNCLEAR_HEARING.1 + 0.1) as f32);
        assert_eq!(a.target_name(), "attentive");
        a.missed();
        assert_eq!(a.target_name(), "skepticism");
        run(&mut a, (MISSED_HOLD + 0.1) as f32);
        a.set_state("idle", true, false);
        a.missed();
        a.unclear_hearing();
        assert_eq!(a.target_name(), "muted");
        a.set_state("asleep", false, false);
        a.missed();
        a.unclear_hearing();
        assert_eq!(a.target_name(), "asleep");
    }

    #[test]
    fn dreaming_is_a_demo_gesture_and_does_not_leak_into_live_sleep() {
        let mut a = Anim::new(7);
        a.set_state("idle", false, false);
        a.express("dreaming", 3.0);
        assert_eq!(a.target_name(), "neutral");
        a.preview_dreaming();
        assert_eq!(a.target_name(), "dreaming");
        a.set_state("asleep", false, false);
        assert_eq!(a.target_name(), "asleep");
    }

    #[test]
    fn committed_dream_flutters_during_the_wake_blink_after_the_first_frame() {
        let mut ordinary = Anim::new(7);
        let mut dreamed = Anim::new(7);
        for a in [&mut ordinary, &mut dreamed] {
            a.set_state("asleep", false, false);
            a.set_lid(Some(true), false, false);
            let dark = a.frame(0.1, |a| a.dream_completed_on_open());
            assert_eq!((dark.l.glow, dark.r.glow), (0.0, 0.0));
            a.set_lid(Some(false), false, false);
            a.set_state("waking", false, false);
        }
        let first = dreamed.frame(0.0, |a| a.dream_completed_on_open());
        assert_eq!(first, ordinary.frame(0.0, |_| {}));
        assert!(first.l.h < 0.03 && first.r.h < 0.03);
        let blink = run(&mut ordinary, 0.75);
        let flutter = run(&mut dreamed, 0.75);
        assert!(flutter.l.x > blink.l.x + 0.018);
        assert_eq!(run(&mut dreamed, 0.55), run(&mut ordinary, 0.55));

        ordinary.set_lid(Some(true), false, false);
        ordinary.set_lid(Some(false), false, true);
        ordinary.frame(0.0, |a| a.dream_completed_on_open());
        assert!(ordinary.lid_wake.is_none());
    }

    #[test]
    fn reclose_cancels_wake_and_unknown_lid_blanks_unless_recovered() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, false);
        assert!(run(&mut a, 0.26).l.h > 0.08);
        a.set_lid(Some(true), true, false);
        let blank = a.frame(0.0, |_| {});
        assert_eq!((blank.l.glow, blank.r.glow), (0.0, 0.0));
        assert!(a.lid_wake.is_none());

        assert!(!a.set_lid(None, false, true));
        assert!(a.set_lid(None, true, false));
        assert!(a.frame(0.0, |_| {}).l.glow > 0.0);
        assert!(a.lid_wake.is_none());
        a.set_lid(Some(false), false, false);
        assert!(a.frame(0.0, |_| {}).l.h < 0.03);
    }

    #[test]
    fn lid_reopened_after_a_stall_plays_the_whole_wake() {
        let mut a = Anim::new(7);
        a.set_lid(Some(true), false, false);
        a.set_state("asleep", false, false);
        let first = a.frame(1.7, |a| {
            a.set_lid(Some(false), false, false);
            a.set_state("waking", false, false);
        });
        assert!(first.l.h < 0.03 && first.l.glow > 0.0);
        let squint = run(&mut a, 0.26);
        assert!(squint.l.h > squint.r.h + 0.03);
        let perk = run(&mut a, 0.70);
        assert!(perk.l.h > 0.30);
    }

    #[test]
    fn touch_glance_applies_without_waiting_for_idle_gaze() {
        let mut a = Anim::new(7);
        a.set_state("idle", false, false);
        run(&mut a, 1.6); // fully awake, before the first idle look
        let normal = run(&mut a, 0.25);
        a.glance(0.0, -0.9, 0.75);
        let glance = run(&mut a, 0.25);
        assert!(glance.l.y < normal.l.y - 0.03);
    }

    #[test]
    fn a_blink_closes_from_above_in_each_expression() {
        for &id in ExprId::PUBLIC {
            let name = id.name();
            let pose = id.pose();
            for open in [pose.l, pose.r] {
                for x in [-0.7, -0.35, 0.0, 0.35, 0.7] {
                    let mut last = aperture(open, x);
                    for step in 1..=20 {
                        let mut eye = open;
                        close_lids(&mut eye, step as f32 / 20.0);
                        let gap = aperture(eye, x);
                        assert!(gap <= last + 1e-6, "{name} blink opens at x={x} on step {step}");
                        last = gap;
                    }
                    assert!(last <= 0.028, "{name} blink stays open at x={x}");
                }
            }
        }
        let mut eye = eye::preset("neutral").unwrap().l;
        close_lids(&mut eye, 1.0);
        assert!(eye.top < 0.0 && eye.bot < 0.0);
    }

    #[test]
    fn a_tipped_eye_shuts_into_a_level_line_where_its_gap_was() {
        // Where the middle of the closing gap sits on the screen, for lids
        // `e` on an eye placed and tipped like `at`.
        fn gap(e: Eye, at: Eye) -> [f32; 2] {
            let g = 0.5 * e.h * (e.top + e.top_curve + e.bot + e.bot_curve);
            [at.x - g * at.tilt.sin(), at.y + g * at.tilt.cos()]
        }
        for name in ["neutral", "concern", "curiosity"] {
            let pose = eye::preset(name).unwrap();
            for open in [pose.l, pose.r] {
                let mut shut = open;
                close_lids(&mut shut, 1.0);
                assert!(shut.tilt.abs() < 1e-6, "{name} shuts level");
                let mut half = open;
                close_lids(&mut half, 0.6);
                let (a, b) = (gap(half, open), gap(half, half));
                assert!((a[0] - b[0]).abs() < 0.02 && (a[1] - b[1]).abs() < 0.02, "{name} gap slides");
            }
        }
    }

    #[test]
    fn a_round_eye_closes_to_points_not_a_flat_cup() {
        // Lidless shapes (surprise, curiosity's round eye) bow their lids as
        // they come down, so the corners close ahead of the middle.
        for (name, open) in [("surprise", eye::preset("surprise").unwrap().l), ("curiosity", eye::preset("curiosity").unwrap().l)] {
            let mut half = open;
            close_lids(&mut half, 0.45);
            let middle = aperture(half, 0.0);
            let corner = aperture(half, 0.8);
            assert!(middle > 0.05, "{name} still open in the middle");
            // Straight lids leave the corners about 0.7 of the middle here.
            assert!(corner < 0.62 * middle, "{name} corners close first ({corner} vs {middle})");
        }
        // A lid that already shapes the eye keeps its own curve.
        let joy = eye::preset("joy").unwrap().l;
        let mut squint = joy;
        close_lids(&mut squint, 0.3);
        assert!(squint.bot_curve >= joy.bot_curve - 1e-6);
    }

    #[test]
    fn listening_picks_up_presence_and_returns_to_it_after_touch() {
        let mut a = Anim::new(7);
        a.set_state("listening", false, false);
        run(&mut a, 0.3);
        a.set_gaze(0.8, 0.25);
        run(&mut a, 0.21);
        assert!(a.gaze.position(a.t)[0] > 0.7);

        a.glance(0.0, -0.9, 0.7);
        run(&mut a, 0.23);
        assert!(a.gaze.position(a.t)[1] < -0.8);
        a.set_gaze(-0.8, 0.25);
        a.set_state("attending", false, false);
        run(&mut a, 0.20);
        assert!(a.gaze.position(a.t)[1] < -0.8);
        run(&mut a, 0.52);
        assert!(a.gaze.position(a.t)[0] < -0.7);
    }

    #[test]
    fn small_camera_jitter_preserves_the_subject_and_real_motion_redirects() {
        let mut a = Anim::new(7);
        a.set_state("listening", false, false);
        a.set_gaze(0.35, 0.0);
        run(&mut a, 0.3);
        for i in 0..16 {
            a.frame(1.0 / 20.0, |a| a.set_gaze(if i % 2 == 0 { 0.41 } else { 0.35 }, 0.0));
        }
        assert_eq!(a.presence.unwrap().0, [0.35, 0.0]);
        a.frame(1.0 / 20.0, |a| a.set_gaze(-0.35, 0.0));
        run(&mut a, 0.18);
        assert!(a.gaze.position(a.t)[0] < -0.30);
    }

    #[test]
    fn losing_a_camera_fixation_returns_the_eyes_to_center() {
        let mut a = Anim::new(34);
        a.set_state("idle", false, false);
        a.set_gaze(0.8, 0.0);
        a.frame(0.2, |_| {});
        a.frame(0.2, |_| {});
        assert!(a.gaze.position(a.t)[0] > 0.5);
        a.frame(0.02, |a| a.clear_gaze());
        assert!(a.presence.is_none());
        a.frame(0.2, |_| {});
        assert!(a.gaze.position(a.t)[0].abs() < 0.04);
    }

    #[test]
    fn going_to_sleep_does_not_snap_a_blink_open() {
        let mut a = Anim::new(7);
        a.set_state("idle", false, false);
        run(&mut a, 1.0);
        a.blink = Some(Blink {
            start: a.t - 0.085,
            close: 0.075,
            hold: 0.035,
            open: 0.120,
            stagger: 0.01,
            depth: 1.0,
            repeat: true,
        });
        let peak = a.frame(0.0, |_| {});
        assert!(aperture(peak.l, 0.0) < 0.025);
        let interrupted = a.frame(0.0, |a| a.set_state("asleep", false, false));
        assert_eq!(aperture(peak.l, 0.0), aperture(interrupted.l, 0.0));
        run(&mut a, 6.0);
        assert!(a.blink.is_none() && !a.blink_repeat);
    }

    #[test]
    fn starting_or_interrupting_a_reply_reacquires_the_listener() {
        let mut a = Anim::new(17);
        for next in ["speaking", "attending"] {
            a.set_state("thinking", false, false);
            run(&mut a, 0.4);
            assert!(a.gaze.position(a.t)[0].abs() >= 0.45);
            a.set_state(next, false, false);
            run(&mut a, 0.21);
            assert!(a.gaze.position(a.t)[0].abs() < 0.04);
        }
    }

    #[test]
    fn voice_accents_attack_release_and_expire() {
        let mut a = Anim::new(17);
        a.set_state("speaking", false, false);
        a.set_level(1.0);
        run(&mut a, 0.1);
        assert!(a.level > 0.8);
        a.set_level(0.0);
        run(&mut a, 0.1);
        assert!(a.level > 0.35 && a.level < 0.6);
        a.set_level(1.0);
        a.frame(0.6, |_| {}); // stale after a rendering hitch
        assert!(a.level < 0.35);
        a.set_state("attending", false, false);
        run(&mut a, 0.65);
        assert!(a.level < 0.01);
    }

    #[test]
    fn fresh_motion_events_after_a_stall_keep_their_lifetime() {
        let mut a = Anim::new(7);
        a.set_state("speaking", false, false);
        a.glance(-0.9, 0.0, 0.4);
        a.set_level(1.0);
        a.express("joy", 0.2);
        run(&mut a, 0.1);
        let resumed = a.frame(0.8, |a| {
            a.set_level(0.8);
            a.glance(0.8, 0.0, 0.4);
            a.express("surprise", 0.2);
        });
        assert_eq!(a.target_name(), "surprise");
        assert!(resumed.l.y > -0.03, "the expired joy pose should have recovered");
        assert!(a.level < 0.03, "the expired speech accent should have decayed");
        run(&mut a, 0.18);
        assert!(a.gaze.position(a.t)[0] > 0.7 && a.level > 0.7);
        assert_eq!(a.target_name(), "surprise");
        run(&mut a, 0.15);
        assert_eq!(a.target_name(), "speaking");
    }

    #[test]
    fn a_due_blink_can_wait_briefly_for_a_pause_in_speech() {
        let mut a = Anim::new(17);
        a.set_state("speaking", false, false);
        a.blink_next = 0.1;
        a.set_level(1.0);
        run(&mut a, 0.16);
        assert!(a.blink.is_none());
        a.set_level(0.0);
        run(&mut a, 0.19);
        assert!(a.blink.is_some());
    }

    #[test]
    fn state_and_overlays_pick_the_expression() {
        let mut a = Anim::new(1);
        a.set_state("listening", false, false);
        assert_eq!(a.target_name(), "listening");
        a.express("joy", 2.0);
        assert_eq!(a.target_name(), "joy");
        a.set_state("listening", true, false);
        assert_eq!(a.target_name(), "muted");
        a.set_state("listening", false, true);
        assert_eq!(a.target_name(), "dazed");
        a.set_state("asleep", false, true);
        assert_eq!(a.target_name(), "asleep");
    }

    #[test]
    fn only_resting_eyes_ask_for_attention() {
        let mut a = Anim::new(1);
        a.set_lid(Some(false), false, true);
        for state in ["idle", "drowsy", "asleep"] {
            a.set_state(state, false, false);
            assert!(a.resting(), "{state}");
        }
        for state in ["waking", "attending", "listening", "thinking", "speaking"] {
            a.set_state(state, false, false);
            assert!(!a.resting(), "{state}");
        }
        a.set_state("idle", true, false);
        assert!(!a.resting(), "muted: M lets it listen again");
        a.set_state("idle", false, true);
        assert!(!a.resting(), "unwell");
        a.set_state("idle", false, false);
        a.set_link_lost(true);
        assert!(!a.resting(), "no brainstem to hear the key");
        a.set_link_lost(false);
        a.set_lid(Some(true), false, false);
        assert!(!a.resting(), "a dark panel");
    }

    #[test]
    fn a_missed_hearing_squints_briefly_and_never_while_asleep_or_muted() {
        let mut a = Anim::new(1);
        a.set_lid(Some(false), false, true);
        a.set_state("attending", false, false);
        a.missed();
        assert_eq!(a.target_name(), "skepticism");
        run(&mut a, (MISSED_HOLD + 0.1) as f32);
        assert_eq!(a.target_name(), "attentive");
        a.set_state("idle", true, false);
        a.missed();
        assert_eq!(a.target_name(), "muted");
        a.set_state("asleep", false, false);
        a.missed();
        assert_eq!(a.target_name(), "asleep");
    }

    #[test]
    fn emotion_expires() {
        let mut a = Anim::new(1);
        a.set_state("idle", false, false);
        a.express("surprise", 1.0);
        run(&mut a, 0.5);
        assert_eq!(a.target_name(), "surprise");
        run(&mut a, 1.0);
        assert_eq!(a.target_name(), "neutral");
    }

    #[test]
    fn unknown_expression_is_ignored() {
        let mut a = Anim::new(1);
        a.set_state("idle", false, false);
        a.express("no-such-face", 5.0);
        assert_eq!(a.target_name(), "neutral");
    }

    #[test]
    fn losing_the_brainstem_shows() {
        let mut a = Anim::new(1);
        a.set_state("idle", false, false);
        a.set_link_lost(true);
        assert_eq!(a.target_name(), "dazed");
    }

    #[test]
    fn slow_and_uneven_frames_do_not_break_the_motion() {
        // A software renderer may manage ten frames a second, or two.
        for &dt in &[0.1f32, 0.07, 0.033, 0.5] {
            let mut a = Anim::new(5);
            a.set_state("idle", false, false);
            let mut steps = 0;
            for (i, state) in ["idle", "attending", "thinking", "speaking", "idle"].iter().cycle().take(25).enumerate()
            {
                a.set_state(state, false, false);
                a.set_gaze(if i % 2 == 0 { 0.9 } else { -0.9 }, 0.5);
                for _ in 0..(4.0 / dt) as usize {
                    // Uneven: every third frame takes twice as long.
                    steps += 1;
                    let p = a.frame(if steps % 3 == 0 { dt * 2.0 } else { dt }, |_| {});
                    for e in [p.l, p.r] {
                        assert!(e.to_array().iter().all(|v| v.is_finite()), "not finite at dt={dt}");
                        assert!(e.x.abs() < 1.3 && e.y.abs() < 0.6, "eye left the screen at dt={dt}: {e:?}");
                        assert!(e.w < 0.7 && e.h < 0.7, "eye grew out of bounds at dt={dt}: {e:?}");
                    }
                }
            }
        }
    }

    fn awake(seed: u32) -> Anim {
        let mut a = Anim::new(seed);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        run(&mut a, 2.0);
        a
    }

    #[test]
    fn joy_arrives_with_a_little_hop() {
        let mut a = awake(3);
        a.express("joy", 5.0);
        let settled = eye::preset("joy").unwrap().l.y;
        let mut highest = f32::MIN;
        for _ in 0..30 {
            highest = highest.max(a.frame(1.0 / 60.0, |_| {}).l.y);
        }
        assert!(highest > settled + 0.015, "no hop: {highest} vs {settled}");
        let rest = run(&mut a, 1.5).l.y;
        assert!((rest - settled).abs() < 0.03, "the hop should settle");
    }

    #[test]
    fn being_spoken_to_gets_a_bounce_but_an_interruption_does_not() {
        let mut a = awake(3);
        a.set_state("attending", false, false);
        run(&mut a, 0.17);
        assert!(a.step_life(0.0, 0.0, true, true).bounce > 0.015);
        a.set_state("thinking", false, false);
        run(&mut a, 1.0);
        a.set_state("attending", false, false);
        run(&mut a, 0.17);
        assert_eq!(a.step_life(0.0, 0.0, true, true).bounce, 0.0);
    }

    #[test]
    fn surprise_pops_and_blinks_twice() {
        let mut a = awake(5);
        a.blink_next = a.t + 10.0;
        a.express("surprise", 3.0);
        let mut blinks = 0;
        let mut closed = false;
        for _ in 0..60 {
            a.frame(1.0 / 60.0, |_| {});
            let shut = a.blink.as_ref().is_some_and(|b| b.closure(a.t, 0.0) > 0.9);
            if shut && !closed {
                blinks += 1;
            }
            closed = shut;
        }
        assert_eq!(blinks, 2);
    }

    #[test]
    fn the_startle_recoils_and_both_blinks_close_regardless_of_seed() {
        for seed in 1..=16 {
            let mut a = awake(seed);
            a.blink_next = a.t + 10.0;
            a.express("surprise", 3.0);
            let mut full_blinks = 0;
            let mut shut = false;
            let mut pop: f32 = 0.0;
            let mut recoil: f32 = 0.0;
            for _ in 0..60 {
                a.frame(1.0 / 60.0, |_| {});
                let life = a.step_life(0.0, 0.0, true, true);
                pop = pop.max(life.pop);
                recoil = recoil.max(life.recoil);
                let closed = a.blink.as_ref().is_some_and(|b| b.closure(a.t, 0.0) > 0.9);
                full_blinks += usize::from(closed && !shut);
                shut = closed;
            }
            assert!(pop > 0.95 && recoil > 0.85, "seed {seed}: pop {pop}, recoil {recoil}");
            assert_eq!(full_blinks, 2, "seed {seed}");
        }
    }

    #[test]
    fn audited_motion_yields_to_engagement_and_scales_with_intensity() {
        let mut a = awake(3);
        a.gaze_next = a.t + 20.0;
        a.blink_next = a.t + 10.0;
        a.express("sick", 3.0);
        a.express("dreaming", 3.0);
        assert_eq!(a.target_name(), "neutral");
        a.audition(ExprId::Embarrassed, 0.5, 3.0);
        run(&mut a, 0.8);
        assert_eq!(a.target_name(), "embarrassed");
        assert!((a.gaze_bias[0] + 0.195).abs() < 0.002);
        assert!((a.head - 0.0355).abs() < 0.01);
        assert!((a.breath_scale - 0.89).abs() < 0.001);
        assert!((a.blink_interval() - 0.88).abs() < 0.001);
        assert!((a.blink_next - a.t) < 8.2);
        let half = blend_pose(ExprId::Neutral.pose(), ExprId::Embarrassed.pose(), 0.5);
        assert_eq!(a.spring.target, half.to_array());
        a.glance(0.9, 0.0, 0.7);
        run(&mut a, 0.55);
        assert!(a.gaze_bias[0].abs() < 0.003);
        assert!(a.gaze.position(a.t)[0] > 0.8);
        run(&mut a, 2.2);
        assert_eq!(a.target_name(), "neutral");
        assert_eq!(a.spring.target, ExprId::Neutral.pose().to_array());
        assert!((a.breath_scale - 1.0).abs() < 0.03);
    }

    /// Every tuned name, taken from the table so each later batch is pinned without a list here.
    fn tuned_ids() -> Vec<ExprId> {
        let ids: Vec<_> = ExprId::ALL.iter().copied().filter(|id| id.is_tuned()).collect();
        assert!(ids.len() >= 19, "the tuned set only grows");
        ids
    }

    #[test]
    fn express_accepts_the_tuned_names_and_still_refuses_concepts_and_dreaming() {
        for id in tuned_ids() {
            let mut a = awake(5);
            a.gaze_next = a.t + 20.0;
            a.blink_next = a.t + 10.0;
            a.express(id.name(), 1.5);
            run(&mut a, 0.6);
            assert_eq!(a.target_name(), id.name());
            run(&mut a, 2.5);
            assert_ne!(a.target_name(), id.name(), "{} outlives its hold", id.name());
        }
        for refused in ["sick", "dreaming", "no-such-name"] {
            let mut a = awake(5);
            a.express(refused, 1.5);
            run(&mut a, 0.6);
            assert_eq!(a.target_name(), "neutral", "{refused}");
        }
    }

    #[test]
    fn express_with_no_options_matches_plain_express() {
        let mut a = awake(5);
        a.express_with("joy", 3.0, None, None);
        let held = a.emotion.as_ref().unwrap();
        assert_eq!((held.id, held.intensity, held.blend), (ExprId::Joy, 1.0, None));
        run(&mut a, 1.0);
        assert_eq!(a.spring.target, ExprId::Joy.pose().to_array());
        assert_eq!(a.spring.intensity, 1.0);
        assert_eq!(a.spring.blend, None);
    }

    #[test]
    fn express_intensity_reaches_the_spring_and_scales_from_neutral() {
        for level in [0.55_f32, 0.3] {
            let mut a = awake(5);
            a.gaze_next = a.t + 20.0;
            a.blink_next = a.t + 10.0;
            a.express_with("embarrassed", 3.0, Some(f64::from(level)), None);
            run(&mut a, 0.8);
            assert_eq!(a.spring.intensity, level);
            let want = blend_pose(ExprId::Neutral.pose(), ExprId::Embarrassed.pose(), level);
            assert_eq!(a.spring.target, want.to_array());
            assert!((a.acting_intensity - level).abs() < 1e-6);
            assert!((a.blink_interval() - mix(1.0, ExprId::Embarrassed.motion().blink_interval, level)).abs() < 1e-5);
        }
    }

    #[test]
    fn express_intensity_is_clamped_or_ignored() {
        let held = |v: Option<f64>| {
            let mut a = awake(5);
            a.express_with("joy", 3.0, v, None);
            a.emotion.as_ref().unwrap().intensity
        };
        assert_eq!(held(Some(7.0)), 1.0);
        assert_eq!(held(Some(-2.0)), 0.0);
        assert_eq!(held(Some(f64::NAN)), 1.0);
        assert_eq!(held(Some(f64::INFINITY)), 1.0);
        assert_eq!(held(None), 1.0);
    }

    #[test]
    fn express_blend_mixes_then_intensity_scales_from_neutral() {
        let mut a = awake(5);
        a.express_with("joy", 3.0, Some(0.5), Some(("sad", 0.25)));
        assert_eq!(a.emotion.as_ref().unwrap().blend, Some((ExprId::Sad, 0.25)));
        run(&mut a, 1.0);
        assert_eq!(a.spring.blend, Some((ExprId::Sad, 0.25)));
        assert_eq!(a.spring.intensity, 0.5);
        let mixed = blend_pose(ExprId::Joy.pose(), ExprId::Sad.pose(), 0.25);
        let want = blend_pose(ExprId::Neutral.pose(), mixed, 0.5);
        assert_eq!(a.spring.target, want.to_array());
        // Mix out of range is clamped.
        for (given, want) in [(3.0, 1.0), (-1.0, 0.0)] {
            let mut a = awake(5);
            a.express_with("joy", 3.0, None, Some(("sad", given)));
            assert_eq!(a.emotion.as_ref().unwrap().blend, Some((ExprId::Sad, want)));
        }
    }

    #[test]
    fn express_invalid_blend_is_dropped_and_the_pose_plays_alone() {
        for blend in [("sick", 0.5), ("dreaming", 0.5), ("joy", 0.5), ("sad", f64::NAN), ("sad", f64::INFINITY)] {
            let mut a = awake(5);
            a.express_with("joy", 3.0, Some(0.7), Some(blend));
            let held = a.emotion.as_ref().unwrap();
            assert_eq!((held.id, held.blend), (ExprId::Joy, None), "{blend:?}");
            assert_eq!(held.intensity, 0.7);
        }
    }

    #[test]
    fn express_unknown_main_name_is_ignored_with_options() {
        for name in ["sick", "dreaming", "no-such-name"] {
            let mut a = awake(5);
            a.express_with(name, 1.5, Some(0.5), Some(("sad", 0.5)));
            run(&mut a, 0.6);
            assert!(a.emotion.is_none(), "{name}");
            assert_eq!(a.target_name(), "neutral", "{name}");
        }
    }

    #[test]
    fn express_hold_is_still_clamped_with_options() {
        let until = |hold: f64| {
            let mut a = awake(5);
            let t = a.t;
            a.express_with("joy", hold, Some(0.5), Some(("sad", 0.5)));
            a.emotion.as_ref().unwrap().until - t
        };
        assert!((until(0.0) - 0.2).abs() < 1e-9);
        assert!((until(9999.0) - 600.0).abs() < 1e-9);
        assert!((until(5.0) - 5.0).abs() < 1e-9);
    }

    #[test]
    fn express_options_keep_the_suppression_rules() {
        let mut a = awake(5);
        a.set_state("asleep", false, false);
        a.express_with("joy", 3.0, Some(0.5), Some(("sad", 0.5)));
        assert!(a.emotion.is_none(), "asleep");
        let mut a = Anim::new(5);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        a.set_lid(Some(true), false, false);
        run(&mut a, 0.5);
        a.express_with("joy", 3.0, Some(0.5), None);
        assert!(a.emotion.is_none(), "dark");
        let mut a = awake(5);
        a.set_state("idle", true, false);
        run(&mut a, 1.0);
        a.express_with("joy", 3.0, Some(0.5), None);
        run(&mut a, 1.0);
        assert_eq!(a.target_name(), "muted");
    }

    #[test]
    fn tuned_poses_keep_the_audition_clamp() {
        for id in tuned_ids() {
            let name = id.name();
            let mut a = awake(5);
            a.gaze_next = a.t + 20.0;
            a.blink_next = a.t + 10.0;
            a.express(name, 3.0);
            run(&mut a, 2.0);
            assert!(a.spring.settled, "{name} still moving");
            assert!(a.spring.guarded, "{name} is not guarded");
            // An extreme pose, as a wide gaze or roll could drive through the spring.
            let mut p = Pose::from_array(&a.spring.x);
            for e in [&mut p.l, &mut p.r] {
                e.w = 3.0;
                e.h = 2.0;
                e.round = 0.5;
                e.glow = 5.0;
                e.x = 1.4_f32.copysign(e.x);
                e.y = 0.8;
            }
            a.spring.x = p.to_array();
            let shown = a.frame(0.0, |_| {});
            for e in [shown.l, shown.r] {
                assert!(e.w <= 1.45 && e.h <= 0.85 && e.round >= 2.0 && e.glow <= 1.3, "{name}: {e:?}");
                assert!(e.x.abs() + e.w < 1.5 && e.y.abs() + e.h < 0.9, "{name}: {e:?}");
            }
        }
    }

    #[test]
    fn tuned_kicks_keep_the_face_active_for_the_kick_window() {
        let kicking: Vec<_> = tuned_ids().into_iter().filter(|id| id.motion().kick != Kick::None).collect();
        assert!(kicking.len() >= 3, "the earlier kicking poses stay kicking");
        for id in kicking {
            let name = id.name();
            let mut a = awake(5);
            a.gaze_next = a.t + 20.0;
            a.blink_next = a.t + 10.0;
            a.express(name, 3.0);
            run(&mut a, 2.0);
            assert_eq!(a.target_name(), name);
            assert!(a.spring.settled, "{name} still moving");
            // Nothing else is busy, so only the kick window can keep the face at full rate.
            a.acted_at = a.t - 0.5;
            assert_eq!(a.activity(), Activity::Active, "{name} inside the kick window");
            a.acted_at = a.t - 0.9;
            assert_ne!(a.activity(), Activity::Active, "{name} after the kick window");
        }
    }

    #[test]
    fn tuned_poses_stay_safe_through_transitions() {
        for id in tuned_ids() {
            let mut a = awake(21);
            a.audition(id, 1.0, 0.9);
            for i in 0..120 {
                let p = a.frame(if i % 3 == 0 { 0.2 } else { 1.0 / 60.0 }, |a| {
                    if i == 5 {
                        a.glance(1.0, 1.0, 0.3);
                    }
                });
                for e in [p.l, p.r] {
                    assert!(e.w > 0.0 && e.h > 0.0 && e.round >= 2.0, "{id:?}: {e:?}");
                    assert!(e.x.abs() + e.w < 1.5 && e.y.abs() + e.h < 0.9, "{id:?}: {e:?}");
                }
            }
        }
    }

    #[test]
    fn audition_render_inputs_are_seeded_and_safe_through_transitions() {
        for &id in ExprId::AUDITIONS {
            let trace = |dts: &[f32]| {
                let mut a = awake(21);
                a.audition(id, 1.0, 0.9);
                (0..120).map(|i| {
                    let dt = dts[i % dts.len()];
                    let p = a.frame(dt, |a| {
                        if i == 5 {
                            a.glance(1.0, 1.0, 0.3);
                        }
                    });
                    for e in [p.l, p.r] {
                        assert!(e.w > 0.0 && e.h > 0.0, "{id:?}: {e:?}");
                        assert!(e.x.abs() + e.w < 1.5 && e.y.abs() + e.h < 0.9, "{id:?}: {e:?}");
                        if a.spring.guarded {
                            assert!(e.round >= 2.0, "{id:?}: {e:?}");
                        }
                    }
                    let uniforms = p.uniforms();
                    assert!(uniforms.iter().all(|value| value.is_finite()), "{id:?}: {uniforms:?}");
                    uniforms.map(f32::to_bits)
                }).collect::<Vec<_>>()
            };
            let uneven = [1.0 / 60.0, 1.0 / 20.0, 1.0 / 60.0, 0.2];
            assert_eq!(trace(&uneven), trace(&uneven), "{id:?}");
            let sixty = trace(&[1.0 / 60.0]);
            assert_ne!(sixty[40], sixty[119], "{id:?}");
        }
    }

    #[test]
    fn a_nod_moves_down_a_shake_swings_both_ways_and_private_states_quiet_them() {
        let mut reference = awake(6);
        let mut nod = awake(6);
        let mut shake = awake(6);
        for a in [&mut reference, &mut nod, &mut shake] {
            a.gaze_next = a.t + 20.0;
            a.blink_next = a.t + 20.0;
        }
        assert!(nod.perform("nod"));
        assert!(shake.perform("shake"));
        assert_eq!(nod.activity(), Activity::Active);
        let mut minimum_shake: f32 = 0.0;
        let mut maximum_shake: f32 = 0.0;
        let mut lowest_nod: f32 = 0.0;
        for _ in 0..65 {
            let center = reference.frame(1.0 / 60.0, |_| {});
            let nod_pose = nod.frame(1.0 / 60.0, |_| {});
            let shake_pose = shake.frame(1.0 / 60.0, |_| {});
            let n = 0.5 * (nod_pose.l.y + nod_pose.r.y - center.l.y - center.r.y);
            let s = 0.5 * (shake_pose.l.x + shake_pose.r.x - center.l.x - center.r.x);
            lowest_nod = lowest_nod.min(n);
            minimum_shake = minimum_shake.min(s);
            maximum_shake = maximum_shake.max(s);
        }
        assert!(lowest_nod < -0.08 && minimum_shake < -0.09 && maximum_shake > 0.09);
        assert!(nod.gesture.is_none() && shake.gesture.is_none());
        shake.set_state("idle", true, false);
        assert!(shake.perform("shake"));
        assert!(shake.gesture.is_none());
        nod.set_state("idle", false, true);
        assert!(nod.perform("nod"));
        assert!(nod.gesture.is_none());
    }

    #[test]
    fn thinking_searches_in_small_hops_on_one_side() {
        let mut a = awake(11);
        a.set_state("thinking", false, false);
        run(&mut a, 0.2);
        let side = a.thought_side;
        let mut spots = Vec::new();
        for _ in 0..30 {
            run(&mut a, 0.1);
            if a.gaze_away && spots.last() != Some(&a.fixation) {
                spots.push(a.fixation);
            }
        }
        assert!(spots.len() >= 3, "it should search, not stare: {spots:?}");
        for spot in spots {
            assert!(spot[0] * side > 0.4 && spot[1] > 0.3, "searching wandered off: {spot:?}");
        }
        assert!(a.head * side > 0.02, "the head leans into the thought");
    }

    #[test]
    fn thinking_looks_as_far_to_the_left_as_to_the_right() {
        let preset = eye::preset("thinking").unwrap();
        let mut reach = Vec::new();
        for side in [-1.0f32, 1.0] {
            let mut a = awake(11);
            a.set_state("thinking", false, false);
            a.thought_side = side;
            a.gaze_next = a.t;
            a.blink_next = a.t + 10.0;
            let p = run(&mut a, 1.5);
            reach.push(side * (p.l.x + p.r.x) / 2.0);
            // The reflected shape is the preset's own, facing the other way.
            let lids = if side * (preset.l.x + preset.r.x) < 0.0 { reflect(preset) } else { preset };
            assert!((a.spring.target[6] - lids.l.top).abs() < 1e-6);
        }
        assert!(reach[0] > 0.06 && (reach[0] - reach[1]).abs() < 0.02, "uneven look away: {reach:?}");
    }

    #[test]
    fn curiosity_tilts_the_head_and_lets_it_go() {
        let mut a = awake(5);
        a.express("curiosity", 1.0);
        run(&mut a, 0.6);
        assert!(a.head < -0.05);
        run(&mut a, 1.5);
        assert!(a.head.abs() < 0.01);
    }

    #[test]
    fn a_long_quiet_spell_makes_the_lids_heavy_and_company_lifts_them() {
        let mut a = awake(9);
        let open = run(&mut a, 0.5);
        a.frame(420.0, |_| {});
        run(&mut a, 0.4); // a blink that was due may finish first
        assert!(a.heaviness > 0.3);
        assert!(a.gesture.is_some_and(|(g, _, _)| g == Gesture::Yawn), "first heaviness brings a yawn");
        a.blink_next = a.t + 10.0;
        let heavy = run(&mut a, 3.0);
        assert!(a.gesture.is_none() && a.heaviness > 0.3, "{:?} {}", a.gesture, a.heaviness);
        assert!(aperture(heavy.l, 0.0) < aperture(open.l, 0.0) * 0.85);
        a.set_gaze(0.1, 0.0);
        run(&mut a, 1.0);
        assert!(a.heaviness < 0.05);
        assert_eq!(a.target_name(), "neutral");
    }

    #[test]
    fn heavy_lids_fade_in_without_a_pop() {
        let life = Life {
            tuck: 0.0, bounce: 0.0, pop: 0.0, recoil: 0.0, nod: 0.0, shake: 0.0,
            content: 0.0, inhale: 0.0, squeeze: 0.0, smile: None,
        };
        let open = eye::preset("neutral").unwrap().l;
        let mut last = aperture(open, 0.0);
        for step in 1..=60 {
            let mut e = open;
            life.apply(&mut e, -1.0, step as f32 * 0.007, 0.0);
            let gap = aperture(e, 0.0);
            assert!(gap <= last + 1e-6 && last - gap < 0.02, "lids jumped at step {step}: {last} -> {gap}");
            last = gap;
        }
        // The breath of a yawn opens heavy lids wide.
        let (mut heavy, mut breath) = (open, open);
        life.apply(&mut heavy, -1.0, 0.42, 0.0);
        Life { inhale: 1.0, ..life }.apply(&mut breath, -1.0, 0.42, 0.0);
        assert!(aperture(breath, 0.0) > aperture(heavy, 0.0) * 1.3);
    }

    #[test]
    fn getting_drowsy_yawns_with_the_eyes_open_first() {
        let mut a = awake(9);
        a.set_state("drowsy", false, false);
        assert_eq!(a.target_name(), "sleepy");
        let stretch = run(&mut a, 0.4);
        assert!(stretch.l.h > eye::preset("neutral").unwrap().l.h, "stretches before the squeeze");
        let squeeze = run(&mut a, 0.8);
        assert!(aperture(squeeze.l, 0.0) < 0.05);
        run(&mut a, 3.0);
        assert!(a.gesture.is_none());
        let sleepy = eye::preset("sleepy").unwrap();
        assert!((a.spring.x[3] - sleepy.l.h).abs() < 0.02, "settles sleepy");
    }

    #[test]
    fn idle_life_includes_a_contented_squint_and_slow_looks_around() {
        let mut a = awake(21);
        let (mut content, mut surveys) = (false, false);
        for _ in 0..1200 {
            run(&mut a, 0.1);
            content |= a.gesture.is_some_and(|(g, _, _)| g == Gesture::Content);
            surveys |= a.surveying && a.gaze.duration >= 0.6;
        }
        assert!(content && surveys);
        assert!(a.perform("content") && a.perform("survey") && !a.perform("juggle"));
        a.set_gaze(0.2, 0.0);
        assert!(a.gesture.is_some(), "company does not cut a gesture off");
        a.express("concern", 1.0);
        assert!(a.gesture.is_none(), "news does");
    }

    #[test]
    fn the_birth_flickers_on_looks_both_ways_tilts_and_settles() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        a.frame(0.0, |a| a.born());
        assert!(a.birth.is_some() && a.lid_wake.is_none());
        let mut glows = Vec::new();
        for _ in 0..(2.3 * 60.0) as usize {
            let p = a.frame(1.0 / 60.0, |_| {});
            assert!(p.l.h < 0.04, "a line of light while it flickers on");
            glows.push(p.l.glow);
        }
        let changes = glows.windows(2).filter(|w| (w[0] - w[1]).abs() > 0.05).count();
        assert!(changes >= 6, "the power-on flickers: {changes} steps");
        assert!(a.activity() == Activity::Active, "drawn smoothly");
        let opened = run(&mut a, 1.8); // t = 4.1
        assert!(opened.l.h > 0.2 && opened.l.glow > 0.8, "{opened:?}");
        let mut xs = Vec::new();
        for _ in 0..(3.0 * 60.0) as usize {
            let p = a.frame(1.0 / 60.0, |_| {});
            xs.push((p.l.x + p.r.x) / 2.0);
        }
        let (left, right) = xs.iter().fold((f32::MAX, f32::MIN), |(lo, hi), &x| (lo.min(x), hi.max(x)));
        assert!(left < -0.10 && right > 0.10, "two slow looks: {left} {right}");
        assert!(a.blink.is_none(), "no ordinary blinks during the ceremony");
        run(&mut a, 0.9); // t = 8.0, past the end
        assert!(a.birth.is_none());
        let settled = run(&mut a, 0.2);
        assert!(settled.l.x.abs() > 0.7 && settled.l.h > 0.25);
    }

    #[test]
    fn being_spoken_to_cuts_the_birth_short_and_a_dark_panel_saves_it_for_later() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        a.frame(0.0, |a| a.born());
        run(&mut a, 3.0);
        a.set_state("attending", false, false);
        run(&mut a, BIRTH_HANDOVER + 0.05);
        assert!(a.birth.is_none(), "handed over to the live face");

        let mut b = Anim::new(7);
        b.set_lid(None, false, true);
        b.set_state("idle", false, false);
        b.frame(0.0, |b| b.born());
        assert!(b.birth.is_none() && b.birth_pending.is_some(), "waits for the panel to light");
        b.frame(5.0, |b| {
            b.set_lid(Some(false), false, true);
        });
        assert!(b.birth.is_some(), "plays on the first lighting");
        run(&mut b, 1.0);
        b.set_lid(Some(true), false, false);
        assert!(b.birth.is_none() && b.birth_pending.is_some(), "closed half-way: kept for next time");

        // Eyes that have been lit a while skip the dark flicker.
        let mut c = awake(4);
        c.frame(0.0, |c| c.born());
        let p = c.frame(1.0 / 60.0, |_| {});
        assert!(p.l.h > 0.2, "no drop back to a line of light");
    }

    #[test]
    fn spoken_expression_follows_clause_onsets_and_stops_on_interrupt() {
        let mut a = awake(41);
        a.set_state("thinking", false, false);
        a.speech_level(0.55, Some("joy"));
        assert_eq!(a.target_name(), "thinking");

        a.set_state("speaking", false, false);
        a.speech_level(0.0, Some("joy"));
        assert_eq!(a.target_name(), "speaking");
        a.speech_level(0.55, Some("joy"));
        assert_eq!(a.target_name(), "joy");
        a.speech_level(0.0, None);
        assert_eq!(a.target_name(), "joy");
        a.speech_level(0.38, Some("concern"));
        assert_eq!(a.target_name(), "concern");

        a.end_reply();
        a.speech_level(0.55, Some("joy"));
        assert_eq!(a.target_name(), "speaking");
        assert_eq!(a.level_target, 0.0);
        a.set_state("listening", false, false);
        a.speech_level(0.55, Some("joy"));
        assert_eq!(a.target_name(), "listening");

        a.set_state("thinking", false, false);
        a.set_state("speaking", false, false);
        a.speech_level(0.38, Some("concern"));
        assert_eq!(a.target_name(), "concern");
        a.set_lid(Some(true), false, false);
        assert_eq!(a.speech_cue, None);
    }

    #[test]
    fn listening_acknowledges_once_and_screen_outcomes_yield_to_conversation() {
        let mut a = awake(42);
        a.set_state("listening", false, false);
        a.screen_outcome(false);
        assert_eq!(a.target_name(), "listening");
        a.unclear_hearing();
        assert!(a.unheard_until > a.t);
        assert_eq!(a.target_name(), "confused");
        a.set_state("thinking", false, false);
        assert!(matches!(a.gesture, Some((Gesture::Acknowledge, _, _))));
        let heard = a.gesture;
        a.set_state("thinking", false, false);
        assert_eq!(a.gesture, heard);

        run(&mut a, 1.5);
        a.set_state("idle", false, false);
        a.screen_outcome(false);
        assert_eq!(a.target_name(), "concern");
        run(&mut a, 0.8);
        assert_eq!(a.target_name(), "neutral");
        a.screen_outcome(true);
        assert!(matches!(a.gesture, Some((Gesture::Acknowledge, _, _))));
    }

    #[test]
    fn a_screen_outcome_lets_a_running_yawn_or_smile_finish() {
        for gesture in [Gesture::Yawn, Gesture::Content] {
            let mut a = awake(42);
            a.start_gesture(gesture);
            run(&mut a, 0.5);
            let running = a.gesture;
            a.screen_outcome(true);
            assert_eq!(a.gesture, running, "{gesture:?}");
            assert!(a.touch.is_some(), "the glance still answers");
        }
    }

    #[test]
    fn a_brief_link_loss_or_dark_panel_does_not_end_the_reply() {
        let mut a = awake(42);
        a.set_state("thinking", false, false);
        a.set_state("speaking", false, false);
        a.speech_level(0.55, Some("joy"));
        a.set_link_lost(true);
        assert_eq!(a.speech_cue, None);
        assert_eq!(a.level_target, 0.0);
        a.set_link_lost(false);
        a.speech_level(0.7, Some("joy"));
        assert!(a.level_target > 0.0);
        assert_eq!(a.target_name(), "joy");

        a.set_lid(Some(true), false, false);
        a.set_lid(Some(false), false, false);
        a.speech_level(0.7, None);
        assert!(a.level_target > 0.0);

        a.end_reply();
        a.speech_level(0.7, None);
        assert_eq!(a.level_target, 0.0, "a real end still holds");
    }

    #[test]
    fn a_source_pose_preview_survives_the_lid_closing() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, true);
        a.enable_concept_preview([false; emote::CHANNELS], true);
        a.set_lid(Some(true), false, false);
        assert!(a.spring.source_pose);
        assert_eq!(a.spring.x, ExprId::Asleep.concept_pose().to_array());
        a.set_lid(Some(false), false, false);
        assert!(a.spring.source_pose);
    }

    #[test]
    fn waiting_for_the_brainstem_looks_calm_not_sick() {
        let mut a = awake(3);
        a.set_link_lost(true);
        a.set_loading(true);
        assert_eq!(a.target_name(), "loading");
        let p = run(&mut a, 1.0);
        assert!(p.l.tilt.abs() < 0.4 && p.l.glow > 0.5, "{p:?}");
        a.set_loading(false);
        assert_eq!(a.target_name(), "dazed", "a minute on, it is unwell");
        a.set_loading(true);
        a.set_state("idle", false, true);
        assert_eq!(a.target_name(), "dazed", "a brainstem that says unwell is believed");
    }

    #[test]
    fn a_dazed_sleep_is_uneven() {
        let mut calm = awake(3);
        let mut sick = awake(3);
        for a in [&mut calm, &mut sick] {
            a.set_state("asleep", false, false);
            run(a, 3.0);
        }
        sick.set_link_lost(true);
        let (p, q) = (run(&mut calm, 0.5), run(&mut sick, 0.5));
        assert_eq!(sick.target_name(), "asleep");
        assert!((q.l.tilt - q.r.tilt).abs() > (p.l.tilt - p.r.tilt).abs() + 0.02, "askew: {q:?}");
    }

    #[test]
    fn breathing_stays_smooth_after_days_awake() {
        let mut a = awake(3);
        a.set_state("asleep", false, false);
        a.frame(3.0 * 86400.0, |_| {});
        let mut heights = Vec::new();
        for _ in 0..120 {
            heights.push(a.frame(1.0 / 60.0, |_| {}).l.h);
        }
        let jumps = heights.windows(2).map(|w| (w[0] - w[1]).abs()).fold(0.0, f32::max);
        assert!(jumps < 0.002, "breath jumped by {jumps}");
        assert!(heights.iter().any(|&h| (h - heights[0]).abs() > 0.0005), "still breathing");
    }

    #[test]
    fn a_flat_battery_weighs_on_the_lids_without_a_yawn_and_company_lifts_them() {
        let mut a = awake(9);
        a.set_drives(Drives { energy: Some(0.1), ..Default::default() });
        a.blink_next = a.t + 30.0;
        a.gesture_next = a.t + 60.0;
        run(&mut a, 20.0);
        assert!(a.heaviness > 0.15 && a.heaviness < 0.3, "{}", a.heaviness);
        assert!(!a.yawned, "tired is not sleepy");
        a.set_gaze(0.1, 0.0);
        run(&mut a, 1.0);
        assert!(a.heaviness < 0.05);
        a.clear_gaze();
        a.set_drives(Drives { energy: Some(1.0), comfort: Some(1.0), ..Default::default() });
        run(&mut a, 20.0);
        assert!(a.heaviness < 0.02, "a full battery leaves the lids alone");
    }

    #[test]
    fn idle_looks_linger_after_a_long_quiet() {
        let count = |arousal: f32| {
            let mut a = awake(13);
            a.set_drives(Drives { arousal: Some(arousal), ..Default::default() });
            let mut moves = 0;
            let mut last = a.fixation;
            for _ in 0..600 {
                run(&mut a, 0.1);
                if a.fixation != last {
                    moves += 1;
                    last = a.fixation;
                }
            }
            moves
        };
        let (sleepy, lively) = (count(0.0), count(1.0));
        assert!(lively as f32 > sleepy as f32 * 1.2, "lively {lively} vs quiet {sleepy}");
    }

    #[test]
    fn a_reunion_after_a_long_time_alone_gets_a_bigger_hello() {
        let hop = |social: f32| {
            let mut a = awake(3);
            a.set_drives(Drives { social: Some(social), ..Default::default() });
            a.set_state("attending", false, false);
            run(&mut a, 0.17);
            (a.step_life(0.0, 0.0, true, true).bounce, a.target_name())
        };
        let (usual, _) = hop(1.0);
        let (keen, _) = hop(0.05);
        assert!(keen > usual * 1.5, "{keen} vs {usual}");
        // Someone sits down after an hour alone: a moment of joy, once.
        let mut a = awake(3);
        a.set_drives(Drives { social: Some(0.1), ..Default::default() });
        a.set_gaze(0.2, 0.0);
        assert_eq!(a.target_name(), "joy");
        a.clear_gaze();
        run(&mut a, 2.0);
        a.set_gaze(0.2, 0.0);
        assert_eq!(a.target_name(), "neutral", "not twice in ten minutes");
    }

    #[test]
    fn typing_eyes_follow_the_cursor_and_look_up_when_paused() {
        let mut a = awake(5);
        a.set_gaze(0.8, 0.3); // someone is there, but Pip is busy typing
        a.set_focus(Some(Focus { kind: FocusKind::Typing, at: [-0.5, -0.7] }));
        run(&mut a, 0.4);
        let p = a.gaze.position(a.t);
        assert!((p[0] + 0.5).abs() < 0.03 && (p[1] + 0.7).abs() < 0.03, "{p:?}");
        // The cursor moves on: the eyes follow after a short reading delay.
        a.set_focus(Some(Focus { kind: FocusKind::Typing, at: [-0.2, -0.7] }));
        a.frame(1.0 / 60.0, |_| {});
        assert!((a.fixation[0] + 0.5).abs() < 1e-6, "not before the saccade delay");
        run(&mut a, 0.4);
        assert!((a.gaze.position(a.t)[0] + 0.2).abs() < 0.03);
        a.set_focus(Some(Focus { kind: FocusKind::Paused, at: [0.0, 0.1] }));
        run(&mut a, 0.4);
        assert!(a.gaze.position(a.t)[1] > 0.0, "looks up at the person");
        // Someone talking to Pip wins over the panel.
        a.set_state("listening", false, false);
        run(&mut a, 0.6);
        assert!(a.gaze.position(a.t)[0] > 0.6, "looks at the speaker");
        a.set_state("idle", false, false);
        a.set_focus(None);
        run(&mut a, 0.1);
        assert!(a.focus.is_none());
    }

    #[test]
    fn typing_behind_a_minimised_terminal_holds_back_idle_performances() {
        let mut a = awake(21);
        a.set_typing_hidden(true);
        for _ in 0..1200 {
            run(&mut a, 0.1);
            assert!(a.gesture.is_none() && !a.surveying);
        }
    }

    #[test]
    fn the_frame_rate_follows_what_is_happening() {
        let mut a = Anim::new(5);
        a.set_lid(Some(true), false, true);
        assert_eq!(a.activity(), Activity::Blank);
        let mut a = awake(5);
        a.set_state("speaking", false, false);
        assert_eq!(a.activity(), Activity::Active);
        a.set_state("idle", false, false);
        a.blink_next = a.t + 60.0;
        a.gesture_next = a.t + 60.0;
        a.gaze_next = a.t + 60.0;
        run(&mut a, 3.0);
        assert_eq!(a.activity(), Activity::Calm);
        a.set_state("asleep", false, false);
        run(&mut a, 6.0);
        assert_eq!(a.activity(), Activity::Asleep);
    }

    #[test]
    fn stays_finite_and_on_screen_for_minutes() {
        let mut a = Anim::new(99);
        for (i, state) in ["idle", "attending", "listening", "thinking", "speaking", "drowsy", "asleep", "waking"]
            .iter()
            .cycle()
            .take(40)
            .enumerate()
        {
            a.set_state(state, false, false);
            a.set_level((i % 3) as f32 * 0.5);
            let p = run(&mut a, 3.0);
            for e in [p.l, p.r] {
                for v in e.to_array() {
                    assert!(v.is_finite());
                }
                assert!(e.x.abs() < 1.6 && e.y.abs() < 1.0, "left the screen in {state}");
                assert!(e.h >= 0.012);
            }
        }
    }

    // ---- Live marks (Effects layer) ----

    fn marks_on(seed: u32) -> Anim {
        let mut a = awake(seed);
        a.set_effects_layer(true);
        a
    }

    /// The emotes the live path would draw. Asking for them is what tells the
    /// face the Effects layer is on, so these helpers put that back as it was:
    /// a test about the layer being off must not switch it on by looking.
    fn live(a: &Anim, name: &str) -> f32 {
        let before = a.effects_called_t.get();
        let value = a.emote_motion(false).map_or(0.0, |(effects, _, _)| effects[emote::channel(name).unwrap()]);
        a.effects_called_t.set(before);
        value
    }

    fn live_is_none(a: &Anim) -> bool {
        let before = a.effects_called_t.get();
        let none = a.emote_motion(false).is_none();
        a.effects_called_t.set(before);
        none
    }

    fn lab(a: &Anim, name: &str) -> f32 {
        a.emote_motion(true).map_or(0.0, |(effects, _, _)| effects[emote::channel(name).unwrap()])
    }

    /// The X or spiral pupil (slot 33 or 34) the eyes would be drawn with,
    /// through the real fade, for the given layers.
    fn pupil(a: &Anim, details: bool, slot: usize) -> f32 {
        let mut decor = decor::Decor::default();
        let layers = decor::Layers { details, ..decor::Layers::default() };
        decor.update(layers, a.decor_motion(false), 1.0);
        decor.uniforms()[slot]
    }

    #[test]
    fn the_face_sees_the_effects_layer_from_the_render_loop_asking_for_emotes() {
        // No call to set_effects_layer: the way the app really runs.
        let mut a = awake(5);
        a.express("sad", 5.0);
        run(&mut a, 1.0);
        let pupils = |a: &Anim| a.decor_motion(false).map_or([0.0; 2], |m| m.special_pupils);
        assert_eq!(pupils(&a), [0.0; 2], "Effects off: nothing until emotes are asked for");
        a.express("ko", 5.0);
        run(&mut a, 1.0);
        assert_eq!(pupils(&a), [0.0; 2], "still off while nothing asks for emotes");
        // The render loop asks for emotes each frame once Effects is on.
        for _ in 0..10 {
            a.frame(0.05, |_| {});
            let _ = a.emote_motion(false);
        }
        assert!(pupils(&a)[0] > 0.0 || pupils(&a)[1] > 0.0, "ko shows its X pupil once the loop asks for emotes");
        // A preview request never turns the layer on.
        let mut b = awake(5);
        b.express("ko", 5.0);
        for _ in 0..10 {
            b.frame(0.05, |_| {});
            let _ = b.emote_motion(true);
        }
        assert_eq!(pupils(&b), [0.0; 2]);
        // And it lapses shortly after the loop stops asking (Effects switched off).
        run(&mut a, 1.0);
        assert_eq!(pupils(&a), [0.0; 2]);
    }

    #[test]
    fn the_blend_field_on_the_bus_only_counts_as_an_object_with_a_name_and_a_mix() {
        use serde_json::json;
        let held = |value: serde_json::Value| {
            let mut a = awake(5);
            a.express_event("concern", 5.0, None, Some(&value));
            a.emotion.as_ref().map(|held| held.blend)
        };
        let some = Some(Some((ExprId::from_name("joy").unwrap(), 0.4)));
        assert_eq!(held(json!({"name": "joy", "mix": 0.4})), some);
        assert_eq!(held(json!({"name": "joy", "mix": 0.4, "extra": {"deep": [1, 2]}})), some, "extra keys are ignored");
        for bad in [json!("joy"), json!(null), json!(7), json!([]), json!({"name": "joy"}), json!({"mix": 0.4}),
                    json!({"name": "joy", "mix": "0.4"}), json!({"name": 5, "mix": 0.4}), json!({"name": "dreaming", "mix": 0.4}),
                    json!({"name": "no-such-face", "mix": 0.4}), json!({"name": "concern", "mix": 0.4})] {
            assert_eq!(held(bad.clone()), Some(None), "{bad}: the pose plays alone");
        }
        let mut a = awake(5);
        a.express_event("concern", 5.0, None, None);
        assert_eq!(a.emotion.as_ref().map(|held| held.blend), Some(None), "no blend field");
    }

    #[test]
    fn allowed_marks_show_live_only_with_the_effects_layer() {
        let mut a = awake(5);
        a.express("bashful", 100.0);
        run(&mut a, 1.0);
        assert_eq!(live(&a, "blush"), 0.0, "layer off: eyes only");
        assert!(live_is_none(&a));
        a.set_effects_layer(true);
        assert!(live(&a, "blush") > 0.9, "authored blush at full strength");
        assert!(lab(&a, "blush") > 0.9, "a preview still shows it");
        a.set_effects_layer(false);
        assert!(live_is_none(&a));
    }

    #[test]
    fn live_marks_fade_in_and_out_rather_than_pop() {
        let mut a = marks_on(5);
        a.express("bashful", 3.0);
        a.frame(0.02, |_| {});
        let early = live(&a, "blush");
        assert!(early < 0.5, "starts faint: {early}");
        run(&mut a, 1.0);
        assert!(live(&a, "blush") > 0.9);
        a.frame(2.0 - 0.1, |_| {});
        a.frame(0.05, |_| {});
        let fading = live(&a, "blush");
        assert!(fading > 0.0 && fading < 0.9, "easing out at the end of the hold: {fading}");
    }

    #[test]
    fn marks_outside_the_allow_list_never_show_live_but_do_in_a_preview() {
        let mut a = marks_on(5);
        a.express("angry", 100.0);
        run(&mut a, 1.0);
        assert!(live(&a, "steam") > 0.0, "steam is allowed");
        for name in ["anger", "shiver", "breath"] {
            assert_eq!(live(&a, name), 0.0, "{name} is stripped live");
            assert!(lab(&a, name) > 0.0, "{name} still previews");
        }
        // Every channel off the list is zero live, whatever the pose.
        for id in ExprId::ALL {
            let mut a = marks_on(5);
            a.audition(*id, 1.0, 100.0);
            run(&mut a, 1.0);
            if let Some((effects, burst, _)) = a.emote_motion(false) {
                assert_eq!(burst, 0.0);
                for (index, value) in effects.iter().enumerate() {
                    let cue = matches!(index, emote::QUESTION | emote::DOTS | emote::BATTERY);
                    assert!(*value == 0.0 || live_channel(index) || cue, "{} {}", id.name(), emote::NAMES[index]);
                }
            }
        }
    }

    #[test]
    fn an_expression_with_only_disallowed_marks_shows_nothing_and_settles() {
        let mut a = marks_on(5);
        a.express("amazed", 100.0);
        run(&mut a, 6.0);
        assert!(a.emote_motion(false).is_none(), "sparkle alone is stripped");
        assert!(a.emote_motion(true).is_some(), "but the preview has it");
        a.express("annoyed", 100.0);
        run(&mut a, 6.0);
        assert!(a.emote_motion(false).is_none());
        assert_eq!(a.activity(), Activity::Calm, "no mark showing, so the face settles");
    }

    #[test]
    fn a_showing_mark_keeps_the_face_active_and_a_hidden_one_does_not() {
        let mut a = marks_on(5);
        a.express("bashful", 100.0);
        run(&mut a, 6.0);
        assert_eq!(a.activity(), Activity::Active, "a mark is on screen");
        a.set_effects_layer(false);
        assert_eq!(a.activity(), Activity::Calm, "layer off: nothing draws, nothing keeps the rate up");
    }

    #[test]
    fn lab_only_poses_never_promote_to_live_marks() {
        for name in LAB_ONLY {
            let id = ExprId::from_name(name).unwrap();
            let mut a = marks_on(5);
            a.audition(id, 1.0, 100.0);
            run(&mut a, 1.0);
            assert!(a.emote_motion(false).is_none(), "{name}");
            assert!(a.emote_motion(true).is_some(), "{name} still previews");
        }
        // Only the public names the director may send reach live marks at all.
        for name in LAB_ONLY {
            assert!(!ExprId::PUBLIC.iter().any(|id| id.name() == name), "{name}");
        }
    }

    #[test]
    fn flame_and_steam_vanish_on_a_fresh_hot_reading_and_return_after() {
        let mut a = marks_on(5);
        a.audition(ExprId::from_name("raging").unwrap(), 1.0, 600.0);
        run(&mut a, 1.0);
        assert!(live(&a, "flame") > 0.9 && live(&a, "steam") > 0.9, "no reading at all: they show");
        a.frame(0.0, hot);
        assert_eq!(live(&a, "flame"), 0.0, "immediately");
        assert_eq!(live(&a, "steam"), 0.0);
        run(&mut a, 3.0);
        assert_eq!(live(&a, "flame"), 0.0, "still hot");
        // A critical reading counts the same.
        a.frame(0.0, |a| a.thermal_budget(Some("critical"), Some("thermal"), true, 12_000.0));
        assert_eq!(live(&a, "steam"), 0.0);
        // Not refreshed: the 12 s ttl lapses and they come back.
        a.frame(12.1, |_| {});
        assert!(live(&a, "flame") < 0.9, "easing back, not popping");
        run(&mut a, 1.0);
        assert!(live(&a, "flame") > 0.9 && live(&a, "steam") > 0.9);
    }

    #[test]
    fn a_revoke_or_a_cooler_reading_lifts_the_flame_guard() {
        for lift in 0..3 {
            let mut a = marks_on(5);
            a.audition(ExprId::from_name("raging").unwrap(), 1.0, 600.0);
            run(&mut a, 1.0);
            a.frame(0.0, hot);
            assert_eq!(live(&a, "flame"), 0.0);
            a.frame(0.5, |a| match lift {
                0 => a.thermal_budget(None, None, false, 0.0),
                1 => a.thermal_budget(Some("warm"), Some("thermal"), true, 12_000.0),
                _ => a.thermal_budget(Some("hot"), Some("battery"), true, 12_000.0),
            });
            run(&mut a, 1.0);
            assert!(live(&a, "flame") > 0.9, "lift {lift}");
        }
    }

    #[test]
    fn x_and_spiral_pupils_stand_down_for_the_honest_cues_and_a_settle() {
        let ko = ExprId::from_name("ko").unwrap();
        let dizzy = ExprId::from_name("dizzy").unwrap();
        for (cause, id, slot) in [("unwell", ko, 33), ("link", ko, 33), ("loading", dizzy, 34), ("unwell", dizzy, 34)] {
            let mut a = marks_on(5);
            a.audition(id, 1.0, 600.0);
            run(&mut a, 1.0);
            assert!(pupil(&a, false, slot) > 0.9, "{cause} before");
            match cause {
                "unwell" => a.set_state("idle", false, true),
                "link" => a.set_link_lost(true),
                _ => {
                    a.set_link_lost(true);
                    a.set_loading(true);
                }
            }
            run(&mut a, 2.0);
            assert_eq!(pupil(&a, false, slot), 0.0, "{cause} while it lasts");
            match cause {
                "unwell" => a.set_state("idle", false, false),
                "link" => a.set_link_lost(false),
                _ => {
                    a.set_loading(false);
                    a.set_link_lost(false);
                }
            }
            run(&mut a, PUPIL_SETTLE as f32 - 1.0);
            assert_eq!(pupil(&a, false, slot), 0.0, "{cause} during the settle");
            run(&mut a, 2.0);
            assert!(pupil(&a, false, slot) > 0.9, "{cause} returns");
        }
    }

    #[test]
    fn with_effects_on_and_details_off_only_x_and_spiral_pupils_show() {
        let id = |name: &str| ExprId::from_name(name).unwrap();
        let mut a = marks_on(5);
        a.audition(id("ko"), 1.0, 600.0);
        run(&mut a, 1.0);
        assert!(pupil(&a, false, 33) > 0.9);
        a.audition(id("dizzy"), 1.0, 600.0);
        run(&mut a, 1.0);
        assert!(pupil(&a, false, 34) > 0.9);
        // Heart and star pupils stay under Eye details.
        let mut love = marks_on(5);
        love.audition(id("love"), 1.0, 600.0);
        run(&mut love, 1.0);
        assert_eq!(pupil(&love, false, 32), 0.0);
        assert!(pupil(&love, true, 32) > 0.9, "details on: as today");
        let mut star = marks_on(5);
        star.audition(id("starstruck"), 1.0, 600.0);
        run(&mut star, 1.0);
        assert_eq!(pupil(&star, false, 35), 0.0);
        assert!(pupil(&star, true, 35) > 0.9);
        // Effects off: nothing from this feature shows.
        a.set_effects_layer(false);
        assert_eq!(pupil(&a, false, 34), 0.0);
        // Effects off, details on: the guard does not apply, as before.
        let mut off = marks_on(5);
        off.audition(ExprId::from_name("ko").unwrap(), 1.0, 600.0);
        run(&mut off, 1.0);
        off.set_link_lost(true);
        run(&mut off, 1.0);
        off.set_link_lost(false);
        off.set_effects_layer(false);
        run(&mut off, 1.5);
        assert!(pupil(&off, true, 33) > 0.9, "effects off: details pupils unguarded");
        // A preview ignores the layer switch and the guards.
        assert_eq!(a.decor_motion(true).unwrap().special_pupils, [0.0; 2]);
    }

    #[test]
    fn heart_eyes_do_not_gate_on_the_effects_layer_when_details_are_on() {
        let mut a = awake(5);
        a.audition(ExprId::from_name("love").unwrap(), 1.0, 600.0);
        run(&mut a, 1.0);
        assert!(pupil(&a, true, 32) > 0.9);
    }

    #[test]
    fn state_true_cues_are_unchanged_by_the_live_marks() {
        let mut a = marks_on(5);
        a.set_state("thinking", false, false);
        run(&mut a, 1.0);
        let (effects, _, _) = a.emote_motion(false).unwrap();
        assert_eq!(effects[emote::DOTS], 0.8);
        assert_eq!(effects.iter().filter(|value| **value > 0.0).count(), 1);
    }
}

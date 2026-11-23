//! Eye acting lives here. Expression springs provide the shape, while gaze,
//! blinks and the speech envelope have their own timing.

use crate::eye::{self, Eye, Pose, M};
use std::f32::consts::TAU;

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
    name: &'static str,
    target: [f32; M],
    settled: bool,
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
    fn new(name: &'static str) -> Self {
        let target = eye::preset(name).unwrap().to_array();
        Self { x: target, v: [0.0; M], name, target, settled: true, damping: CALM, flip: false, flipped: false }
    }

    fn step(&mut self, name: &'static str, dt: f32, omega: f32) {
        if self.name != name || self.flip != self.flipped {
            self.name = name;
            self.flipped = self.flip;
            let pose = eye::preset(name).unwrap();
            self.target = if self.flip { reflect(pose) } else { pose }.to_array();
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
        }
    }
}

const WAKE_DURATION: f32 = 1.23;
/// How long a yawn holds the eyes open before the squeeze.
const YAWN_STRETCH: f32 = 0.9;
/// Spring damping for ordinary moves; lively arrivals overshoot a touch.
const CALM: f32 = 0.82;
const CENTER: [f32; 2] = [0.0, 0.0];

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

/// Expressions that arrive with a little acting rather than a plain glide.
fn lively(name: &str) -> bool {
    matches!(name, "attentive" | "listening" | "thinking" | "joy" | "curiosity" | "surprise" | "skepticism" | "concern")
}

fn damping_for(name: &str) -> f32 {
    match name {
        "joy" | "surprise" => 0.64,
        "attentive" | "curiosity" | "skepticism" => 0.72,
        _ => CALM,
    }
}

/// Small self-contained performances layered over the expression.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Gesture {
    /// A soft, pleased half-close of the lids with a smiling lower lid.
    Content,
    /// A stretch up, a scrunched squeeze, and a slow release.
    Yawn,
}

impl Gesture {
    fn length(self) -> f32 {
        match self {
            Gesture::Content => 2.3,
            Gesture::Yawn => 2.6,
        }
    }
}

/// One frame's worth of acting, worked out once and applied to each eye.
struct Life {
    tuck: f32,
    bounce: f32,
    pop: f32,
    content: f32,
    inhale: f32,
    squeeze: f32,
    /// The joy pose, looked up only while a contented gesture borrows its lids.
    smile: Option<Pose>,
}

impl Life {
    fn apply(&self, e: &mut Eye, side: f32, heaviness: f32, head: f32) {
        let Life { tuck, bounce, pop, content, inhale, squeeze, smile } = *self;
        // The big breath of a yawn opens even heavy lids wide for a moment.
        let heaviness = heaviness * (1.0 - 0.85 * inhale);
        // A tuck before a lively move, hops of delight, and the surprise pop.
        e.h *= 1.0 - 0.06 * tuck + 0.7 * bounce + 0.09 * pop + 0.17 * inhale;
        e.w *= 1.0 + 0.025 * tuck - 0.25 * bounce + 0.06 * pop - 0.05 * inhale;
        e.y += bounce - 0.008 * tuck + 0.04 * inhale - 0.015 * squeeze + 0.012 * content - 0.012 * heaviness;
        e.glow *= (1.0 + 1.2 * bounce + 0.08 * pop + 0.06 * inhale) * (1.0 - 0.10 * squeeze) * (1.0 - 0.12 * heaviness);
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
    let sleepy = eye::preset("sleepy").unwrap();
    let curious = eye::preset("curiosity").unwrap();
    let neutral = eye::preset("neutral").unwrap();
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

pub struct Anim {
    t: f64,
    rng: Rng,
    spring: Spring,
    omega: f32,

    state: String,
    state_at: f64,
    muted: bool,
    unwell: bool,
    /// The brainstem has gone quiet; the face shows it without being told.
    link_lost: bool,
    emotion: Option<(&'static str, f64)>,
    battery_cue: Option<BatteryCue>,
    preview_dream: bool,
    lid_open: bool,
    panel_blank: bool,
    lid_wake: Option<LidWake>,

    gaze: Gaze,
    fixation: [f32; 2],
    gaze_next: f64,
    gaze_away: bool,
    thought_side: f32,
    presence: Option<([f32; 2], f64)>,
    touch: Option<([f32; 2], f64)>,

    blink: Option<Blink>,
    blink_next: f64,
    blink_repeat: bool,
    breath_phase: f32,
    breath_depth: f32,

    level: f32,
    level_target: f32,
    level_at: f64,

    /// The expression last seen arriving, where it came from, and when.
    acting: &'static str,
    acted_from: &'static str,
    acted_at: f64,
    /// The next blink is a quick double, as after a surprise.
    double_blink: bool,
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
}

impl Anim {
    /// Starts with the eyes closed, so the first thing it does is wake.
    pub fn new(seed: u32) -> Anim {
        Anim {
            t: 0.0,
            rng: Rng(seed | 1),
            spring: Spring::new("asleep"),
            omega: 2.5,
            state: "waking".to_string(),
            state_at: 0.0,
            muted: false,
            unwell: false,
            link_lost: false,
            emotion: None,
            battery_cue: None,
            preview_dream: false,
            lid_open: false,
            panel_blank: false,
            lid_wake: None,
            gaze: Gaze::new(),
            fixation: CENTER,
            gaze_next: 2.2,
            gaze_away: false,
            thought_side: 1.0,
            presence: None,
            touch: None,
            blink: None,
            blink_next: 3.0,
            blink_repeat: false,
            breath_phase: 0.0,
            breath_depth: 0.0,
            level: 0.0,
            level_target: 0.0,
            level_at: -10.0,
            acting: "asleep",
            acted_from: "asleep",
            acted_at: -10.0,
            double_blink: false,
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
        }
    }

    pub fn set_state(&mut self, state: &str, muted: bool, unwell: bool) {
        self.preview_dream = false;
        let changed = state != self.state;
        if changed {
            if state == "waking" && self.asleep() {
                self.omega = 2.5;
            }
            if self.state == "speaking" {
                self.emotion = None; // a reply's emotion lasts as long as the reply
                self.level_target = 0.0;
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
        if changed || status_changed {
            self.reset_fixation();
            // Give the first visible look on acknowledging someone time to land.
            if self.engaged() {
                self.blink_next = self.blink_next.max(self.t + 0.9);
                self.blink_repeat = false;
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
        if !self.panel_blank && !self.asleep() && name != "dreaming" {
            if let Some(name) = eye::NAMES.iter().copied().find(|&preset| preset == name) {
                self.emotion = Some((name, self.t + hold.clamp(0.2, 600.0)));
                self.quiet_since = self.t;
                if self.gesture.is_some_and(|(g, _, _)| g == Gesture::Content) {
                    self.gesture = None;
                }
            }
        }
    }

    /// Preview the sleeping flutter while the appliance would be dark.
    pub fn preview_dreaming(&mut self) {
        self.set_state("asleep", false, false);
        self.preview_dream = true;
        self.state_at = self.t;
    }

    pub fn set_low_battery(&mut self, low: bool) {
        if low {
            self.battery_cue.get_or_insert(BatteryCue { next: self.t, until: self.t });
        } else {
            self.battery_cue = None;
        }
    }

    pub fn end_reply(&mut self) {
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
        if self.panel_blank {
            if !was_blank {
                self.spring = Spring::new("asleep");
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
            self.touch = None;
            self.presence = None;
            self.fixation = CENTER;
            self.gaze = Gaze::new();
            self.gesture = None;
            self.heaviness = 0.0;
            self.head = 0.0;
            self.head_v = 0.0;
            self.reset_fixation();
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

    /// Play an idle gesture now: `content`, `yawn`, or `survey` (a slow look
    /// around the room). Returns whether it is one the face knows.
    pub fn perform(&mut self, name: &str) -> bool {
        let gesture = match name {
            "content" => Gesture::Content,
            "yawn" => Gesture::Yawn,
            "survey" => {
                if !self.panel_blank && self.state == "idle" && self.presence.is_none() {
                    self.survey();
                }
                return true;
            }
            _ => return false,
        };
        if !self.panel_blank && !self.asleep() {
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
        self.level_target = ((v - 0.06) / 0.94).clamp(0.0, 1.0).sqrt();
        self.level_at = self.t;
        if self.level_target > 0.0 {
            self.quiet_since = self.t;
        }
    }

    pub fn set_link_lost(&mut self, lost: bool) {
        if self.link_lost != lost {
            self.link_lost = lost;
            self.reset_fixation();
        }
    }

    pub fn asleep(&self) -> bool {
        self.state == "asleep"
    }

    fn dazed(&self) -> bool {
        self.unwell || self.link_lost
    }

    /// The expression the face is heading toward right now.
    pub fn target_name(&self) -> &'static str {
        if self.asleep() {
            return if self.preview_dream { "dreaming" } else { "asleep" };
        }
        if self.dazed() {
            return "dazed";
        }
        if self.muted {
            return "muted";
        }
        if let Some((name, until)) = self.emotion {
            if self.t < until {
                return name;
            }
        }
        if self.state == "idle" && self.battery_cue.as_ref().is_some_and(|cue| self.t < cue.until) {
            return "tired";
        }
        match self.state.as_str() {
            "attending" => "attentive",
            "listening" => "listening",
            "thinking" => "thinking",
            "speaking" => "speaking",
            "drowsy" => "sleepy",
            _ => "neutral",
        }
    }

    /// The shape the spring heads for. A yawn keeps the eyes open for its
    /// stretch, and the drowsy shape arrives behind the squeeze.
    fn shape_name(&self) -> &'static str {
        let name = self.target_name();
        let stretching =
            self.gesture.is_some_and(|(g, at, _)| g == Gesture::Yawn && ((self.t - at) as f32) < YAWN_STRETCH);
        if stretching && matches!(name, "sleepy" | "asleep") {
            "neutral"
        } else {
            name
        }
    }

    /// The thinking preset leans to one side. A thought off to the other
    /// side reflects it, so the look away reads the same either way.
    fn facing_left(&self) -> bool {
        self.shape_name() == "thinking"
            && eye::preset("thinking").is_some_and(|p| (p.l.x + p.r.x) * self.thought_side < -0.01)
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
            let held = self.emotion.map_or(elapsed, |(_, until)| ((until - previous) as f32).clamp(0.0, elapsed));
            self.spring.damping = damping_for(self.target_name());
            self.spring.flip = self.facing_left();
            self.spring.step(self.shape_name(), held, self.omega);
            if held < elapsed {
                self.emotion = None;
                self.spring.damping = damping_for(self.target_name());
                self.spring.flip = self.facing_left();
                self.spring.step(self.shape_name(), elapsed - held, self.omega);
            }
            self.step_voice(elapsed);
        }
        self.t = now;
        receive(self);

        // Notice a new expression arriving, so its entrance can be acted.
        let arriving = self.target_name();
        if arriving != self.acting {
            self.acted_from = self.acting;
            self.acting = arriving;
            self.acted_at = self.t;
            if arriving == "surprise" && !self.panel_blank && self.lid_wake.is_none() {
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
        let t = self.t as f32;
        let gaze = self.step_gaze();
        let wake = self
            .lid_wake
            .as_ref()
            .map(|wake| ((self.t - wake.started) as f32, wake.dreamed))
            .filter(|(elapsed, _)| *elapsed < WAKE_DURATION);
        if wake.is_none() {
            self.lid_wake = None;
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
            }
        }
        // Carry phase across state changes so a breath never jumps mid-cycle.
        let depth = if asleep { 1.0 } else { 0.0 };
        self.breath_depth += (depth - self.breath_depth) * dt;
        self.breath_phase = (self.breath_phase + TAU * mix(0.22, 0.13, self.breath_depth) * dt) % TAU;
        let breath = self.breath_phase.sin();

        let expressive = !asleep && !self.muted && !self.dazed();
        let life = self.step_life(elapsed, dt, expressive, wake.is_none());
        let engaged = self.engaged() && expressive;
        let speech = if expressive { self.level } else { 0.0 };
        let age = (self.t - self.state_at) as f32;
        let drift = [0.006 * (t * 0.081).sin(), 0.004 * (t * 0.107 + 1.0).sin()];
        for (side, e) in [(-1.0f32, &mut p.l), (1.0f32, &mut p.r)] {
            e.h *= 1.0 + mix(0.011, 0.045, self.breath_depth) * breath;
            e.y += mix(0.003, 0.012, self.breath_depth) * breath;
            e.x += gaze[0] * 0.137 + drift[0];
            e.y += gaze[1] * 0.083 + drift[1];
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
            life.apply(e, side, self.heaviness, self.head);

            if !asleep && self.dazed() {
                e.tilt += 0.038 * (1.65 * t + side).sin();
                e.glow *= 0.94 + 0.055 * (2.2 * t + 2.0 * side).sin();
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
        }
        wake.map_or(p, |(elapsed, dreamed)| lid_wake_pose(elapsed, p, dreamed))
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
            0.42 * ease((self.t - self.quiet_since) as f32, 150.0, 480.0)
        } else {
            0.0
        };
        let time_constant = if heavy > self.heaviness { 4.0 } else { 0.35 };
        self.heaviness += (heavy - self.heaviness) * (1.0 - (-elapsed / time_constant).exp());
        if heavy == 0.0 && self.heaviness < 0.05 {
            self.yawned = false;
        }
        if alone && self.gesture.is_none() && self.blink.is_none() {
            if self.heaviness > 0.3 && !self.yawned {
                self.yawned = true;
                self.start_gesture(Gesture::Yawn);
            } else if self.t >= self.gesture_next {
                self.gesture_next = self.t + f64::from(self.rng.range(20.0, 38.0));
                if self.heaviness < 0.2 && self.battery_cue.as_ref().is_none_or(|cue| self.t >= cue.until) {
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
                "curiosity" => -0.075,
                "listening" => 0.028 * self.listen_side,
                "thinking" => 0.04 * self.thought_side,
                _ => 0.0,
            }
        } else {
            0.0
        };
        roll += gesture_side * (0.035 * content + 0.05 * squeeze);
        let steps = (dt / 0.02).ceil().max(1.0);
        let h = dt / steps;
        for _ in 0..steps as usize {
            self.head_v += (70.0 * (roll - self.head) - 13.4 * self.head_v) * h;
            self.head += self.head_v * h;
        }

        let age = (self.t - self.acted_at) as f32;
        let tuck = if expressive && lively(self.acting) {
            anticipation(age) * if self.acting == "surprise" { 1.6 } else { 1.0 }
        } else {
            0.0
        };
        let bounce = match self.acting {
            _ if !expressive => 0.0,
            "joy" => 0.042 * hop(age - 0.07, 0.24, 2.0),
            "attentive" if matches!(self.acted_from, "neutral" | "sleepy" | "tired") => 0.036 * hop(age - 0.04, 0.26, 1.0),
            _ => 0.0,
        };
        let pop = if expressive && self.acting == "surprise" {
            ease(age, 0.04, 0.11) * (1.0 - ease(age, 0.13, 0.45))
        } else {
            0.0
        };
        let smile = if content > 0.0 { eye::preset("joy") } else { None };
        Life { tuck, bounce, pop, content, inhale, squeeze, smile }
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

    fn step_gaze(&mut self) -> [f32; 2] {
        if self.presence.is_some_and(|(_, until)| self.t >= until) {
            self.presence = None;
            if self.follows_presence() {
                self.reset_fixation();
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
                ([subject[0] + correction[0], subject[1] + correction[1]], self.rng.range(0.9, 2.0))
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
            } else if self.gaze_away {
                self.gaze_away = false;
                // Come back from a look around the room as unhurriedly.
                self.surveying = returning;
                (CENTER, self.rng.range(2.5, 5.3))
            } else if self.state == "idle" && self.rng.next() < 0.28 {
                self.survey();
                (self.fixation, (self.gaze_next - self.t) as f32)
            } else {
                self.gaze_away = true;
                let side = if self.rng.next() < 0.5 { -1.0 } else { 1.0 };
                ([side * self.rng.range(0.24, 0.51), self.rng.range(-0.22, 0.29)], self.rng.range(0.75, 1.55))
            };
            self.fixation = point.map(|x| x.clamp(-1.0, 1.0));
            self.gaze_next = self.t + f64::from(wait);
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

    fn step_blink(&mut self) {
        if self.asleep() {
            if self.blink.as_ref().is_some_and(|blink| blink.done(self.t)) {
                self.blink = None;
            }
            self.blink_next = self.t + 2.0;
            return;
        }
        let drowsy = matches!(self.target_name(), "sleepy" | "tired") || self.heaviness > 0.22;
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
            } else {
                self.rng.range(2.5, 5.6)
            };
            self.blink_next = self.t + f64::from(wait);
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
        let soft = !drowsy && !double && self.rng.next() < 0.22;
        let depth = if soft { self.rng.range(0.55, 0.78) } else { 1.0 };
        let pace = if double {
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
        let mut fast = Spring::new("neutral");
        let mut slow = Spring::new("neutral");
        let mut hitch = Spring::new("neutral");
        for _ in 0..18 {
            fast.step("joy", 1.0 / 60.0, 14.0);
        }
        for _ in 0..6 {
            slow.step("joy", 1.0 / 20.0, 14.0);
        }
        hitch.step("joy", 0.3, 14.0);
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
        a.frame(0.1, |a| a.set_low_battery(true));
        assert_eq!(a.target_name(), "joy");
        a.frame(0.1, |a| a.set_state("idle", false, false));
        assert_eq!(a.target_name(), "tired");

        a.frame(0.1, |a| a.set_state("idle", true, false));
        assert_eq!(a.target_name(), "muted");
        a.frame(0.1, |a| a.set_state("idle", true, true));
        assert_eq!(a.target_name(), "dazed");
        a.frame(0.1, |a| {
            a.set_low_battery(false);
            a.set_state("idle", false, false);
        });
        assert_eq!(a.target_name(), "neutral");
    }

    #[test]
    fn battery_cue_uses_six_second_window_and_ignores_repeated_readings() {
        let mut a = Anim::new(7);
        a.set_lid(Some(false), false, true);
        a.set_state("idle", false, false);
        a.frame(0.0, |a| a.set_low_battery(true));
        assert_eq!(a.target_name(), "tired");
        a.frame(5.0, |a| a.set_low_battery(true));
        assert_eq!(a.target_name(), "tired");
        a.frame(1.1, |a| a.set_low_battery(true));
        assert_eq!(a.target_name(), "neutral");
        a.frame(39.0, |a| a.set_low_battery(true));
        assert_eq!(a.target_name(), "tired");
        a.frame(0.1, |a| {
            a.set_lid(Some(true), false, false);
            a.set_state("asleep", false, false);
        });
        let blank = a.frame(0.0, |_| {});
        assert_eq!((blank.l.glow, blank.r.glow), (0.0, 0.0));
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
        for &name in eye::NAMES {
            let pose = eye::preset(name).unwrap();
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
        let life = Life { tuck: 0.0, bounce: 0.0, pop: 0.0, content: 0.0, inhale: 0.0, squeeze: 0.0, smile: None };
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
}

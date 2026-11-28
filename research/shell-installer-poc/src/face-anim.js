/*
 * face-anim.js: a JavaScript port of Pip's face animation.
 *
 * Source of truth: crates/face/src/anim.rs (the spec), eye.rs, pace.rs and
 * audition.rs (preview gestures), resynchronised with the 27 November source.
 * Every layer of Anim::frame (anim.rs 1470-1708) is ported: the exact
 * underdamped spring with intensity and blend targets (anim.rs 55-99) and the
 * safe-area guard for guarded (audition, partial or blended) poses (anim.rs
 * 1685-1700), blinks (the 74/26 lid curve, soft, repeat, startle double blinks
 * with the follow-up, per-expression blink intervals, anim.rs 2013-2087),
 * saccades and idle gaze (with the listening dwell and the idle-glances
 * switch), the reply ladder's look-away after a heard utterance (anim.rs
 * 864-875, 1882-1905), thinking search hops, survey looks, presence, touch
 * glances, panel focus, breathing (with the optional body-effort experiment,
 * anim.rs 1586-1616), expression gaze bias, roll, breath and kicks (startle,
 * laugh, flinch, relief), the speech-level envelope, spoken face cues
 * (anim.rs 1216-1238), the engaged focus pulse, Content, Yawn, Nod, Shake and
 * Acknowledge gestures, screen outcomes, the head roll, heavy lids, drives,
 * low-battery cue, dazed / loading / restless-sleep wobble, the dream flutter,
 * the lid wake, the birth ceremony, Face Lab preview gestures (audition.rs
 * 66-162), activity() with pace::hz, trial_idle(), and the live
 * (preview = false) branches of decor_motion and emote_motion.
 *
 * Presets. Every eye shape comes from the caller's `presets` (Shell Lab's
 * FACE.presets from build.py presets(): expressions.json plus the remaining
 * concept-expressions.json poses with round >= 2, exactly as build.rs builds
 * EXPRESSIONS). What build.py does not carry is ported as data here:
 *   - eye::NAMES order = ExprId::PUBLIC (expressions.json order, minus the
 *     private "loading" and the auditions).
 *   - NATIVE: lively, damping and motion of the 28 native expressions in
 *     crates/face/expressions.json (default_damping 0.82). build.rs 263-329.
 *   - Any other preset name is a concept-only audition with damping 0.82, not
 *     lively. Its motion (build.rs 159-170: half the concept gaze, roll
 *     clamped to 0.12, breath = effects[17] clamped 0.6..1.6) comes from
 *     opts.motion[name] (PipAnim.conceptMotion(row) builds it from a
 *     concept-expressions.json row); without it the motion is neutral.
 *   - Eye::OPEN and Eye::mirror (eye.rs 40-65), pace::hz (pace.rs 24-36).
 *
 * Numerics. Rust does the shape maths in f32 and keeps time (t, breath
 * phase) in f64; this port uses JS doubles throughout, except that the
 * random source rounds exactly like Rust's f32 (Rng::next/range, anim.rs
 * 12-22), f32 constants that gate timing (gesture lengths, the yawn
 * stretch, wake and birth lengths, blink intervals) are compared as f32,
 * and the breath rate is rounded to f32 (anim.rs 1592), so a seed gives the
 * same blink, glance and gesture schedule as the Rust face. Poses agree with
 * the Rust build to about 1e-5.
 *
 * Simplified or skipped:
 *   - Concept preview (enable_concept_preview, anim.rs 787-795; the effect
 *     channels, sway, shiver, bounce and visual kicks of anim.rs 840-842,
 *     1603, 1614, 1623-1634, 1775-1780, 1816-1823; preview_effect_values
 *     1444-1456), the preview branches of decor_motion/emote_motion
 *     (1281-1325) and preview_startle_burst (801-803, which only feeds the
 *     emote preview) need each expression's Face Lab visuals (effects, kick,
 *     brows, mouth), which build.py does not export. Those are the offline
 *     Face Lab audition tools; the live face never enables them. The
 *     concept_preview flag is therefore always false here, and
 *     performPreview('startle-burst') has no visible effect.
 *   - audition::advance scenes (audition.rs 279-401) are offline Face Lab
 *     rehearsals and are not ported; their building blocks (audition,
 *     auditionBlend, previewFlash, previewBlink, performPreview,
 *     enableManualPreviewBlinks) are.
 *   - decorMotion() returns the live decor::Motion fields without the
 *     viseme sample (always viseme::Sample::default() in anim.rs 1296).
 *   - The frame-rate floors in main.rs (1423-1437: panel transitions, an
 *     open dock or app surface at least Calm, present-on-change idle frames
 *     at 5 Hz, the being.face_idle_hz trial, moving decor) are the
 *     renderer's, not the animation's; hz() here is pace::hz alone. The
 *     settled-eye swap suppression (main.rs 1382-1388) only skips presenting
 *     an unchanged image; trialIdle() (anim.rs 854-856) is ported so a page
 *     can apply the same gate.
 *   - Anim::frame takes a `receive` closure (anim.rs 1470-1498) that stamps
 *     events after time has caught up. frame(dt, receive) here takes the
 *     same optional callback; methods called between frames are stamped
 *     with the previous frame's time, exactly as calling them before
 *     Anim::frame does in Rust.
 *   - `touch(x, y, hold)` is not a Rust entry point: the Rust touch is the
 *     glance (anim.rs 1079-1084, face.gaze with a hold, link.rs 596-603).
 *     touch() here is glance() with a default hold of 0.75 s.
 *   - wake() and sleep() are conveniences over set_lid / set_state (anim.rs
 *     1088-1139, 881-972): wake() sets the state (default "idle"), then
 *     opens the lid, which plays the 1.23 s lid wake.
 *   - The #[cfg(test)] helper being_born (anim.rs 749-752) is exposed as
 *     beingBorn() for convenience.
 */
(function (root) {
  'use strict';

  var TAU = Math.PI * 2;
  var fr = Math.fround;

  // eye.rs: the field order of Eye::to_array, and ExprId::PUBLIC.
  var FIELDS = ['x', 'y', 'w', 'h', 'round', 'tilt', 'top', 'top_slant', 'top_curve',
    'bot', 'bot_slant', 'bot_curve', 'glow'];
  var N = 13, M = 26;
  var NAMES = ['neutral', 'attentive', 'listening', 'thinking', 'speaking', 'joy', 'curiosity',
    'surprise', 'skepticism', 'concern', 'sleepy', 'asleep', 'dreaming', 'tired', 'muted', 'dazed'];
  // eye.rs Eye::OPEN.
  var OPEN = { x: -0.80, y: 0.03, w: 0.46, h: 0.33, round: 2.3, tilt: 0.0, top: 1.5, top_slant: 0.0,
    top_curve: 0.0, bot: -1.5, bot_slant: 0.0, bot_curve: 0.0, glow: 1.0 };

  // crates/face/expressions.json: everything but the eyes, which come from presets.
  var DEFAULT_DAMPING = fr(0.82);
  var NATIVE = {
    neutral: {}, attentive: { lively: true, damping: 0.72 }, listening: { lively: true },
    thinking: { lively: true }, speaking: {}, joy: { lively: true, damping: 0.64 },
    curiosity: { lively: true, damping: 0.72 },
    surprise: { lively: true, damping: 0.64, motion: { kick: 'startle' } },
    skepticism: { lively: true, damping: 0.72 }, concern: { lively: true }, sleepy: {}, asleep: {},
    dreaming: {}, tired: {}, muted: {}, dazed: {}, loading: { private: true },
    sad: { audition: true, motion: { gaze: [0.0, -0.33], blink_interval: 1.18, breath: 1.24 } },
    laugh: { audition: true, motion: { kick: 'laugh', breath: 1.32, blink_interval: 0.8 } },
    love: { audition: true, motion: { gaze: [0.0, 0.23], roll: -0.037, blink_interval: 1.26, breath: 1.15 } },
    embarrassed: { audition: true, motion: { gaze: [-0.39, -0.3], roll: 0.071, blink_interval: 0.76, breath: 0.78 } },
    scared: { audition: true, motion: { gaze: [-0.12, 0.3], roll: -0.025, kick: 'flinch', blink_interval: 1.45, breath: 0.72 } },
    annoyed: { audition: true, motion: { gaze: [0.16, -0.04], roll: 0.018, blink_interval: 1.16, breath: 0.67 } },
    smug: { audition: true, motion: { gaze: [-0.12, 0.1], roll: -0.075, blink_interval: 1.13 } },
    confused: { audition: true, motion: { gaze: [-0.28, 0.18], roll: -0.105, blink_interval: 0.9 } },
    wink: { audition: true, motion: { roll: -0.055, blink_interval: 1.24 } },
    bored: { audition: true, motion: { gaze: [0.32, -0.15], blink_interval: 1.38, breath: 1.38 } },
    relieved: { audition: true, motion: { kick: 'relief', blink_interval: 1.34, breath: 1.5 } },
  };
  // build.rs: an absent motion field takes these defaults.
  function motionFrom(m) {
    m = m || {};
    return {
      gaze: [fr(m.gaze ? m.gaze[0] : 0.0), fr(m.gaze ? m.gaze[1] : 0.0)],
      roll: fr(m.roll == null ? 0.0 : m.roll),
      blink_interval: fr(m.blink_interval == null ? 1.0 : m.blink_interval),
      breath: fr(m.breath == null ? 1.0 : m.breath),
      kick: m.kick || 'none',
    };
  }
  /** build.rs 159-170: the motion of a concept-only audition from its concept-expressions.json row. */
  function conceptMotion(row) {
    var c = function (v, lo, hi) { return Math.min(Math.max(fr(v), fr(lo)), fr(hi)); };
    return {
      gaze: [fr(0.5 * fr(row.motion[0])), fr(0.5 * fr(row.motion[1]))],
      roll: c(row.motion[2], -0.12, 0.12),
      blink_interval: 1.0,
      breath: c(row.effects[17], 0.6, 1.6),
      kick: 'none',
    };
  }

  // anim.rs 102-117 constants.
  var WAKE_DURATION = fr(1.23);
  var BIRTH = fr(7.6);
  var BIRTH_HANDOVER = fr(0.4);
  var BIRTH_LOOKS = fr(3.9);
  var BIRTH_PENDING = 600.0;
  var REUNION_GAP = 600.0;
  var YAWN_STRETCH = fr(0.9);
  var MISSED_HOLD = 1.1;
  function center() { return [0.0, 0.0]; }

  // ---- eye helpers (eye.rs) ----
  function eyeFrom(a, o) {
    var e = {};
    for (var i = 0; i < N; i++) e[FIELDS[i]] = a[o + i];
    return e;
  }
  function eyeArray(e, out, o) {
    for (var i = 0; i < N; i++) out[o + i] = e[FIELDS[i]];
  }
  function copyEye(e) {
    var c = {};
    for (var i = 0; i < N; i++) c[FIELDS[i]] = e[FIELDS[i]];
    return c;
  }
  function with_(e, changes) {
    var c = copyEye(e);
    for (var k in changes) c[k] = changes[k];
    return c;
  }
  function mirror(e) {
    var c = copyEye(e);
    c.x = -e.x; c.tilt = -e.tilt; c.top_slant = -e.top_slant; c.bot_slant = -e.bot_slant;
    return c;
  }
  function sym(l) { return { l: l, r: mirror(l) }; }
  function poseFrom(a) { return { l: eyeFrom(a, 0), r: eyeFrom(a, N) }; }
  function poseArray(p) {
    var out = new Array(M);
    eyeArray(p.l, out, 0);
    eyeArray(p.r, out, N);
    return out;
  }
  function uniforms(p, out) {
    out = out || new Float32Array(32);
    var eyes = [p.l, p.r];
    for (var i = 0; i < 2; i++) {
      var e = eyes[i], o = i * 16;
      out[o] = e.x; out[o + 1] = e.y; out[o + 2] = e.w; out[o + 3] = e.h;
      out[o + 4] = e.round; out[o + 5] = e.tilt; out[o + 6] = e.top; out[o + 7] = e.top_slant;
      out[o + 8] = e.top_curve; out[o + 9] = e.bot; out[o + 10] = e.bot_slant; out[o + 11] = e.bot_curve;
      out[o + 12] = e.glow; out[o + 13] = 0; out[o + 14] = 0; out[o + 15] = 0;
    }
    return out;
  }

  // ---- pace.rs 24-36 ----
  var ACTIVITY_HZ = { blank: 5, asleep: 15, calm: 30, active: 60 };
  function hz(activity, software) {
    var r = ACTIVITY_HZ[activity];
    return software ? Math.min(r, 20) : r;
  }

  // ---- anim.rs 10-23: xorshift32 with f32 output ----
  function Rng(seed) { this.s = seed >>> 0; }
  Rng.prototype.next = function () {
    var s = this.s;
    s = (s ^ (s << 13)) >>> 0;
    s = (s ^ (s >>> 17)) >>> 0;
    s = (s ^ (s << 5)) >>> 0;
    this.s = s;
    return (s >>> 8) / 16777216;
  };
  Rng.prototype.range = function (lo, hi) {
    var l = fr(lo), h = fr(hi);
    return fr(l + fr(fr(h - l) * this.next()));
  };

  // ---- small maths (anim.rs 119-126, 217-219, 384-398) ----
  function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }
  function ease(t, start, end) {
    var x = clamp((t - start) / (end - start), 0.0, 1.0);
    return x * x * (3.0 - 2.0 * x);
  }
  function mix(a, b, amount) { return a + (b - a) * amount; }
  // mix in f32, for values that gate timing.
  function mixf(a, b, amount) { return fr(fr(a) + fr(fr(fr(b) - fr(a)) * fr(amount))); }
  function blendEye(a, b, amount) {
    var c = {};
    for (var i = 0; i < N; i++) { var k = FIELDS[i]; c[k] = mix(a[k], b[k], amount); }
    return c;
  }
  function blendPoseF(a, b, amount) {
    var l = {}, r = {};
    for (var i = 0; i < N; i++) { var k = FIELDS[i]; l[k] = mixf(a.l[k], b.l[k], amount); r[k] = mixf(a.r[k], b.r[k], amount); }
    return { l: l, r: r };
  }
  function blendPose(a, b, amount) { return { l: blendEye(a.l, b.l, amount), r: blendEye(a.r, b.r, amount) }; }
  function distance(a, b) { return Math.max(Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1])); }
  function same(a, b) { return a[0] === b[0] && a[1] === b[1]; }
  function sameBlend(a, b) { return a === b || (a != null && b != null && a[0] === b[0] && a[1] === b[1]); }
  // anim.rs 44-46
  function reflect(p) { return { l: mirror(p.r), r: mirror(p.l) }; }
  function pulse(age, start, peak, end) { return ease(age, start, peak) * (1.0 - ease(age, peak, end)); }

  // ---- anim.rs 137-180: close along the eye's own lid curves ----
  function closeLids(e, amount) {
    var takeUp = ease(amount, 0.0, 0.23);
    var closure = clamp((amount - 0.10) / 0.90, 0.0, 1.0);
    var top = mix(e.top, Math.min(e.top, 1.05), takeUp);
    var bot = mix(e.bot, Math.max(e.bot, -1.05), takeUp);
    var clearTop = ease(e.top + e.top_curve, 0.6, 1.0);
    var clearBot = ease(-(e.bot + e.bot_curve), 0.6, 1.0);
    var arch = Math.max(0.85 - e.top_curve, 0.0) * takeUp * clearTop;
    var sag = Math.max(0.6 + e.bot_curve, 0.0) * takeUp * clearBot;
    top = top - arch;
    bot = bot + sag;
    e.top_curve += arch;
    e.bot_curve -= sag;
    var sliver = Math.min(0.014 / (2.1 * Math.max(e.h, 0.01)), 0.05);
    var upper = closure * 0.74;
    var lower = closure * (0.26 - sliver);
    e.top = mix(top, bot, upper);
    e.bot = mix(bot, top, lower);
    var ts = e.top_slant, bs = e.bot_slant;
    e.top_slant = mix(ts, bs, upper);
    e.bot_slant = mix(bs, ts, lower);
    var tc = e.top_curve, bc = e.bot_curve;
    e.top_curve = mix(tc, bc, upper);
    e.bot_curve = mix(bc, tc, lower);
    var level = ease(amount, 0.30, 1.0);
    if (level > 0.0 && e.tilt !== 0.0) {
      var gap = 0.5 * e.h * (e.top + e.top_curve + e.bot + e.bot_curve);
      var tilt = e.tilt * (1.0 - level);
      var s0 = Math.sin(e.tilt), c0 = Math.cos(e.tilt);
      var s1 = Math.sin(tilt), c1 = Math.cos(tilt);
      e.x += gap * (s1 - s0);
      e.y += gap * (c0 - c1);
      e.tilt = tilt;
    }
    e.glow *= 1.0 - 0.20 * ease(amount, 0.55, 1.0);
  }

  // anim.rs 182-185
  function blinkClosure(t, close, hold, open) {
    var down = clamp(t / close, 0.0, 1.0);
    return (down * (2.0 - down)) * (1.0 - ease(t, close + hold, close + hold + open));
  }

  // anim.rs 187-215
  function Blink(o) {
    this.start = o.start; this.close = o.close; this.hold = o.hold; this.open = o.open;
    this.stagger = o.stagger; this.depth = o.depth; this.repeat = o.repeat;
  }
  Blink.prototype.elapsed = function (now) { return now - this.start; };
  Blink.prototype.closure = function (now, side) {
    var t = this.elapsed(now) - Math.max(side * this.stagger, 0.0);
    return this.depth * blinkClosure(t, this.close, this.hold, this.open);
  };
  // Compared in f32 like anim.rs 210-212, so the blink ends on the same frame.
  Blink.prototype.done = function (now) {
    return fr(this.elapsed(now)) >= fr(fr(fr(this.close + this.hold) + this.open) + fr(Math.abs(this.stagger)));
  };

  // anim.rs 223-251: an interruptible movement between two fixations.
  function Gaze() { this.from = center(); this.to = center(); this.start = 0.0; this.duration = fr(0.08); }
  Gaze.prototype.position = function (now) {
    var p = ease(now - this.start, 0.0, this.duration);
    return [mix(this.from[0], this.to[0], p), mix(this.from[1], this.to[1], p)];
  };
  Gaze.prototype.track = function (target, now, unhurried) {
    var current = this.position(now);
    if (!same(target, this.to)) {
      // A copy: the caller adds bias and preview offsets to `current` in place.
      this.from = [current[0], current[1]];
      this.to = [target[0], target[1]];
      this.start = now;
      // f32 like anim.rs 247: activity() compares it with an f32 age.
      var reach = fr(Math.min(fr(distance(this.from, target)), fr(1.35)));
      this.duration = Math.max(fr(fr(0.070) + fr(fr(0.095) * fr(Math.sqrt(reach)))), fr(unhurried));
    }
    return current;
  };

  // anim.rs 278-296 with bus::body_drives (null means the body cannot tell).
  function tiredness(d) {
    var flat = d.energy == null ? 0.0 : clamp((0.6 - d.energy) / 0.5, 0.0, 1.0);
    var hot = d.comfort == null ? 0.0 : clamp((0.6 - d.comfort) / 0.6, 0.0, 1.0);
    return Math.min(0.20 * flat + 0.08 * hot, 0.26);
  }
  function gazeTempo(d) { return d.arousal == null ? 1.0 : mix(1.4, 0.85, clamp(d.arousal, 0.0, 1.0)); }
  function eagerness(d) { return d.social == null ? 0.0 : clamp((0.6 - d.social) / 0.6, 0.0, 1.0); }

  // anim.rs 317-333
  var FLICKER = [[0.0, 0.0], [0.35, 0.55], [0.42, 0.0], [0.80, 0.0], [0.86, 0.8], [0.93, 0.25],
    [1.18, 0.0], [1.26, 0.9], [1.33, 0.45], [1.52, 1.0], [1.58, 0.7], [1.80, 1.0]];
  function flicker(t) {
    t = fr(t);
    for (var i = FLICKER.length - 1; i >= 0; i--) if (t >= fr(FLICKER[i][0])) return FLICKER[i][1];
    return 0.0;
  }

  function dreamFlutter(e, amount) {
    e.x += 0.022 * amount;
    e.top += 0.075 * amount;
    e.glow *= 1.0 + 0.07 * amount;
  }
  function hop(age, period, count) {
    if (age <= 0.0 || age >= period * count) return 0.0;
    return Math.abs(Math.sin(Math.PI * age / period)) * Math.exp(-age / period);
  }
  function anticipation(age) { return ease(age, 0.0, 0.05) * (1.0 - ease(age, 0.05, 0.15)); }
  // anim.rs 402-422
  var GESTURE_LENGTH = { content: fr(2.3), yawn: fr(2.6), nod: fr(0.68), acknowledge: fr(0.56), shake: fr(0.82) };

  // decor.rs 39-50
  var NO_DECOR = { loading: 1, muted: 1, dazed: 1, asleep: 1, sleepy: 1, dreaming: 1, tired: 1 };

  // audition.rs 33-162: Face Lab preview gestures and their timed movement.
  var PREVIEW_GESTURES = ['blink', 'nod', 'shake', 'double-take', 'giggle', 'gasp', 'poke', 'eye-roll',
    'look-around', 'jump', 'wiggle', 'tremble', 'startle-burst'];
  var PREVIEW_DURATION = { nod: 0.9, shake: 0.9, giggle: 0.9, 'double-take': 1.1, 'eye-roll': 1.3,
    'look-around': 1.8, 'scene-look-around': 1.4, jump: 0.5, wiggle: 1.0, tremble: 1.2 };
  function previewMotion(g, age) {
    var duration = PREVIEW_DURATION[g];
    if (duration == null) return null;
    duration = fr(duration);
    age = fr(age);
    if (!(age >= 0.0 && age < duration)) return null;
    var p = fr(age / duration);
    var m = { x: 0.0, y: 0.0, roll: 0.0, gaze: [0.0, 0.0] };
    var s;
    switch (g) {
      case 'nod': s = Math.sin(p * TAU * 2.0) * (1.0 - p); m.y = -0.045 * s; m.gaze[1] = -0.5 * s; break;
      case 'shake': s = Math.sin(p * TAU * 3.0) * (1.0 - p); m.x = 0.05 * s; m.gaze[0] = 0.9 * s; m.roll = 0.05 * s; break;
      case 'giggle': m.y = Math.abs(Math.sin(p * TAU * 3.0)) * 0.04 * (1.0 - p); break;
      case 'double-take': m.gaze[0] = p < fr(0.35) ? -1.0 : p < fr(0.6) ? 1.0 : 0.0; break;
      case 'eye-roll': {
        var a = p * TAU * 1.1 - Math.PI * 0.5;
        m.gaze = [1.1 * Math.cos(a), 0.9 * Math.sin(a) + 0.2];
        break;
      }
      case 'look-around': case 'scene-look-around':
        m.gaze = [p < 0.25 ? -1.0 : p < fr(0.6) ? 1.0 : p < fr(0.8) ? -0.4 : 0.0, p < fr(0.6) ? 0.1 : 0.0];
        break;
      case 'jump': m.y = Math.abs(Math.sin(p * Math.PI)) * 0.12 * (1.0 - 0.4 * p); break;
      case 'wiggle': s = Math.sin(p * TAU * 4.0) * (1.0 - p); m.x = 0.03 * s; m.roll = 0.10 * s; break;
      case 'tremble': {
        var edge = Math.min(Math.min(age, duration - age) / 0.08, 1.0);
        m.x = edge * (0.010 * Math.sin(53.0 * age) + 0.005 * Math.sin(91.0 * age));
        m.y = edge * (0.006 * Math.sin(47.0 * age) + 0.004 * Math.sin(82.0 * age));
        break;
      }
    }
    return m;
  }

  // anim.rs 439-496: one frame's acting, applied to each eye.
  function applyLife(life, e, side, heaviness, head) {
    var tuck = life.tuck, bounce = life.bounce, pop = life.pop, content = life.content,
      inhale = life.inhale, squeeze = life.squeeze, smile = life.smile;
    var recoil = life.recoil || 0.0, nod = life.nod || 0.0, shake = life.shake || 0.0;
    heaviness = heaviness * (1.0 - 0.85 * inhale);
    e.h *= 1.0 - 0.06 * tuck + 0.7 * bounce + 0.09 * pop + 0.17 * inhale;
    e.w *= 1.0 + 0.025 * tuck - 0.25 * bounce + 0.06 * pop - 0.05 * inhale;
    e.y += bounce - 0.008 * tuck + 0.04 * inhale - 0.015 * squeeze + 0.012 * content - 0.012 * heaviness;
    e.glow *= (1.0 + 1.2 * bounce + 0.08 * pop + 0.06 * inhale) * (1.0 - 0.10 * squeeze) * (1.0 - 0.12 * heaviness);
    if (recoil !== 0.0 || nod !== 0.0 || shake !== 0.0) {
      e.x += 0.11 * shake;
      e.y += nod - 0.047 * recoil;
      e.h *= 1.0 - 0.09 * recoil + 0.3 * nod;
      e.w *= 1.0 - 0.05 * recoil + 0.035 * side * shake;
      e.tilt += 0.035 * shake;
    }
    if (smile) {
      var joy = side < 0.0 ? smile.l : smile.r;
      var amount = 0.70 * content;
      var keys = ['top', 'top_slant', 'top_curve', 'bot', 'bot_slant', 'bot_curve', 'round', 'tilt'];
      for (var i = 0; i < keys.length; i++) e[keys[i]] = mix(e[keys[i]], joy[keys[i]], amount);
      closeLids(e, 0.18 * content);
    }
    if (squeeze > 0.0) {
      e.bot_curve += 0.35 * squeeze;
      e.tilt -= side * 0.04 * squeeze;
      closeLids(e, 0.86 * squeeze);
    }
    if (heaviness > 0.0) {
      var a = (0.1 + 0.8 * heaviness) * ease(heaviness, 0.0, 0.08);
      e.tilt *= 1.0 - 0.8 * Math.min(2.0 * a, 1.0);
      e.top_slant -= side * 0.3 * a;
      closeLids(e, a);
    }
    if (head !== 0.0) {
      var s = Math.sin(head), c = Math.cos(head);
      var x = e.x, y = e.y;
      e.x = x * c - y * s;
      e.y = x * s + y * c;
      e.tilt += head;
    }
  }

  // ---- Spring (anim.rs 25-100) ----
  function Spring(anim, name) {
    this.anim = anim;
    var target = anim.preset(name).map(fr);
    this.x = target.slice();
    this.v = new Array(M).fill(0.0);
    this.name = name;
    this.intensity = 1.0;
    this.blend = null; // [name, weight]
    this.target = target;
    this.settled = true;
    this.guarded = false;
    this.damping = DEFAULT_DAMPING;
    this.flip = false;
    this.flipped = false;
  }
  /** step(name, dt, omega[, intensity, blend]): blend is null or [name, weight]. */
  Spring.prototype.step = function (name, dt, omega, intensity, blend) {
    if (intensity === undefined) intensity = 1.0;
    if (blend === undefined) blend = null;
    var anim = this.anim;
    if (this.name !== name || this.intensity !== intensity || !sameBlend(this.blend, blend) || this.flip !== this.flipped) {
      this.guarded = this.guarded || anim.isAudition(name) || intensity !== 1.0 || blend != null;
      this.name = name;
      this.intensity = intensity;
      this.blend = blend ? [blend[0], blend[1]] : null;
      this.flipped = this.flip;
      var pose = anim.poseF(name);
      if (blend) pose = blendPoseF(pose, anim.poseF(blend[0]), blend[1]);
      if (this.flip) pose = reflect(pose);
      this.target = poseArray(intensity !== 1.0 ? blendPoseF(anim.poseF('neutral'), pose, intensity) : pose).map(fr);
      this.settled = false;
    }
    if (this.settled || dt === 0.0) return;
    dt = fr(dt); omega = fr(omega); var dmp = fr(this.damping);
    var decay = fr(dmp * omega);
    var frequency = fr(fr(Math.sqrt(fr(1.0 - fr(dmp * dmp)))) * omega);
    var arg = fr(frequency * dt);
    var sin = fr(Math.sin(arg)), cos = fr(Math.cos(arg));
    var envelope = fr(Math.exp(fr(-decay * dt)));
    var travel = fr(fr(envelope * sin) / frequency);
    var forward = fr(fr(envelope * cos) + fr(decay * travel));
    var backward = fr(fr(envelope * cos) - fr(decay * travel));
    var acceleration = fr(fr(fr(-omega) * omega) * travel);
    var settled = true;
    for (var i = 0; i < M; i++) {
      var offset = fr(this.x[i] - this.target[i]);
      var displacement = fr(fr(forward * offset) + fr(travel * this.v[i]));
      this.v[i] = fr(fr(acceleration * offset) + fr(backward * this.v[i]));
      this.x[i] = fr(this.target[i] + displacement);
      settled = settled && Math.abs(displacement) < 0.00002 && Math.abs(this.v[i]) < 0.00015;
    }
    this.settled = settled;
    if (settled) {
      this.x = this.target.slice();
      this.v = new Array(M).fill(0.0);
      this.guarded = anim.isAudition(this.name) || this.intensity !== 1.0 || this.blend != null;
    }
  };

  // ---- birth and lid wake (anim.rs 338-371, 499-529) ----
  function birthPose(anim, t, ready) {
    var sleepy = anim.pose('sleepy'), curious = anim.pose('curiosity');
    var line = with_(OPEN, { h: 0.016 + 0.012 * ease(t, 0.3, 2.4), glow: 0.42 * flicker(t) });
    var lines = sym(line);
    var out = [lines.l, lines.r];
    var readies = [ready.l, ready.r], sleepies = [sleepy.l, sleepy.r], curiouses = [curious.l, curious.r];
    var look = -ease(t, 3.9, 4.7) * (1.0 - ease(t, 4.9, 5.6)) + ease(t, 5.5, 6.2) * (1.0 - ease(t, 6.3, 7.0));
    var tilt = ease(t, 6.5, 6.95) * (1.0 - ease(t, 7.05, BIRTH));
    var head = -0.08 * tilt;
    for (var i = 0; i < 2; i++) {
      var lag = 0.1 * i;
      var opened = blendEye(out[i], sleepies[i], ease(t, 2.4 + lag, 3.1 + lag));
      var e = blendEye(opened, readies[i], ease(t, 3.0 + lag, 3.9 + lag));
      e = blendEye(e, curiouses[i], 0.55 * tilt);
      e.x += 0.30 * look;
      e.y -= 0.03 * Math.abs(look);
      e.tilt += 0.05 * look;
      var near = 1.0 + 0.06 * (2.0 * i - 1.0) * look;
      e.w *= near;
      e.h *= near;
      closeLids(e, blinkClosure(t - 5.05, 0.13, 0.07, 0.22));
      if (head !== 0.0) {
        var s = Math.sin(head), c = Math.cos(head), x = e.x, y = e.y;
        e.x = x * c - y * s;
        e.y = x * s + y * c;
        e.tilt += head;
      }
      out[i] = e;
    }
    return { l: out[0], r: out[1] };
  }

  function lidWakePose(anim, t, ready, dreamed) {
    var closed = sym(with_(OPEN, { h: 0.016, glow: 0.34 }));
    var sleepy = anim.pose('sleepy'), curious = anim.pose('curiosity'), neutral = anim.pose('neutral');
    var l = blendEye(closed.l, sleepy.l, ease(t, 0.09, 0.44));
    var r = blendEye(closed.r, sleepy.r, ease(t, 0.18, 0.53));
    var peer = ease(t, 0.30, 0.48) * (1.0 - ease(t, 0.69, 0.88));
    var perk = ease(t, 0.68, 0.88);
    l = blendEye(l, blendEye(neutral.l, curious.l, 0.64), perk);
    r = blendEye(r, blendEye(neutral.r, curious.r, 0.56), perk);
    l.x -= 0.052 * peer;
    r.x -= 0.052 * peer;
    l.y -= 0.018 * peer;
    r.y -= 0.018 * peer;
    closeLids(l, blinkClosure(t - 0.605, 0.080, 0.042, 0.154));
    closeLids(r, blinkClosure(t - 0.618, 0.080, 0.042, 0.168));
    if (dreamed) {
      var pairs = [[t, l], [t - 0.028, r]];
      for (var i = 0; i < 2; i++) {
        var age = pairs[i][0];
        dreamFlutter(pairs[i][1], ease(age, 0.68, 0.755) * (1.0 - ease(age, 0.765, 0.91)));
      }
    }
    var settle = ease(t, 0.90, WAKE_DURATION);
    return { l: blendEye(l, ready.l, settle), r: blendEye(r, ready.r, settle) };
  }

  // ---- Anim (anim.rs 538-2088) ----
  function Anim(presets, seed, software, motion) {
    if (!presets) throw new Error('PipAnim.create needs presets');
    var all = NAMES.concat(['loading']);
    for (var i = 0; i < all.length; i++) {
      var p = presets[all[i]];
      if (!p || p.length !== M) throw new Error('preset missing or not 26 numbers: ' + all[i]);
    }
    this.presets = presets;
    // Per-expression data (the non-eye part of EXPRESSIONS).
    this.info = {};
    for (var name in presets) {
      var native = NATIVE[name];
      var extra = motion && motion[name];
      this.info[name] = native ? {
        lively: !!native.lively, damping: fr(native.damping == null ? DEFAULT_DAMPING : native.damping),
        audition: !!native.audition, motion: motionFrom(extra || native.motion),
      } : { lively: false, damping: DEFAULT_DAMPING, audition: true, motion: motionFrom(extra) };
    }
    this.software = !!software;
    this.t = 0.0;
    this.rng = new Rng(((seed >>> 0) | 1) >>> 0);
    this.spring = new Spring(this, 'asleep');
    this.omega = 2.5;
    this.state = 'waking';
    this.state_at = 0.0;
    this.reply_ladder = false;
    this.speech_end = null;
    this.muted = false;
    this.unwell = false;
    this.link_lost = false;
    this.emotion = null; // {id, until, intensity, blend}
    this.speech_cue = null;
    this.speech_ended = true;
    this.screen_concern_until = 0.0;
    this.unheard_until = 0.0;
    this.battery_cue = null; // {next, until}
    this.preview_dream = false;
    this.preview_flash = null; // [name, until]
    this.preview_burst = null;
    this.preview_gesture = null; // [name, at]
    this.preview_gaze = [0.0, 0.0];
    this.lid_open = false;
    this.panel_blank = false;
    this.lid_wake = null; // {started, dreamed}
    this.gaze = new Gaze();
    this.gaze_bias = [0.0, 0.0];
    this.fixation = center();
    this.gaze_next = 2.2;
    this.gaze_away = false;
    this.idle_glances = true;
    this.thought_side = 1.0;
    this.presence = null; // [point, until]
    this._touch = null; // [point, until]
    this.blink = null;
    this.blink_next = 3.0;
    this.blink_repeat = false;
    this.preview_blink_manual = false;
    this.breath_phase = 0.0;
    this.breath_depth = 0.0;
    this.breath_scale = 1.0;
    this.breath_experiment = false;
    this.breath_effort = 0.0;
    this.breath_effort_target = 0.0;
    this.level = 0.0;
    this.level_target = 0.0;
    this.level_at = -10.0;
    this.acting = 'asleep';
    this.acting_intensity = 1.0;
    this.acted_from = 'asleep';
    this.acted_at = -10.0;
    this.double_blink = false;
    this.startle_followup = false;
    this.head = 0.0;
    this.head_v = 0.0;
    this.listen_side = 1.0;
    this.gesture = null; // [kind, at, side]
    this.gesture_next = 16.0;
    this.quiet_since = 0.0;
    this.heaviness = 0.0;
    this.yawned = false;
    this.thought_spot = center();
    this.search_next = 0.0;
    this.surveying = false;
    this.drives = { energy: null, arousal: null, social: null, comfort: null };
    this.reunion = 0.0;
    this.reunited_at = -Infinity;
    this.birth = null; // {started, cut}
    this.birth_pending = null;
    this.lit_at = 0.0;
    this.loading = false;
    this.focus = null; // {kind: 'typing'|'watching'|'paused', at: [x, y]}
    this.focus_seen = center();
    this.focus_next = 0.0;
    this.typing_hidden = false;
    this._u = new Float32Array(32);
  }

  var A = Anim.prototype;

  A.preset = function (name) { return this.presets[name]; };
  A.pose = function (name) { return poseFrom(this.presets[name]); };
  A.poseF = function (name) { return poseFrom(this.presets[name].map(fr)); };
  A.isAudition = function (name) { return this.info[name].audition; };
  A.motion = function (name) { return this.info[name].motion; };

  // anim.rs 726-747
  A.born = function () {
    if (this.panel_blank) {
      this.birth_pending = this.t;
    } else if (!this.birth) {
      var skip = this.t - this.lit_at > 1.5 ? BIRTH_LOOKS : 0.0;
      this.startBirth(skip);
    }
  };
  A.startBirth = function (skip) {
    this.birth_pending = null;
    this.birth = { started: this.t - skip, cut: null };
    this.lid_wake = null;
    this.blink = null;
    this.blink_repeat = false;
    this.double_blink = false;
    this.gesture = null;
    this.blink_next = this.t + BIRTH + 0.8;
    this.quiet_since = this.t;
  };
  A.beingBorn = function () { return this.birth != null; };

  /** drives: {energy, arousal, social, comfort}, each 0..1 or null/absent. */
  A.setDrives = function (d) {
    d = d || {};
    this.drives = {
      energy: d.energy == null ? null : d.energy,
      arousal: d.arousal == null ? null : d.arousal,
      social: d.social == null ? null : d.social,
      comfort: d.comfort == null ? null : d.comfort,
    };
  };
  // anim.rs 758-764: the body.breath experiment.
  A.enableEffortBreathing = function () { this.breath_experiment = true; };
  /** effort: 0..1, or null when the body cannot tell. */
  A.setBreathEffort = function (effort) { this.breath_effort_target = effort == null ? 0.0 : effort; };
  A.setLoading = function (on) { this.loading = !!on; };

  function sameFocusKind(a, b) { return (a ? a.kind : null) === (b ? b.kind : null); }
  // anim.rs 773-785
  A.setFocus = function (focus) {
    if (focus) focus = { kind: focus.kind, at: [focus.at[0], focus.at[1]] };
    else focus = null;
    if (!sameFocusKind(focus, this.focus)) {
      this.focus_next = this.t;
      if (!focus) this.resetFixation();
    }
    this.focus = focus;
  };
  A.setTypingHidden = function (on) { this.typing_hidden = !!on; };

  // anim.rs 797-820: Face Lab preview controls.
  A.previewFlash = function (name, seconds) {
    if (this.info[name]) this.preview_flash = [name, this.t + seconds];
  };
  A.previewStartleBurst = function () { this.preview_burst = this.t; };
  A.previewBlink = function () {
    this.blink = new Blink({ start: this.t, close: fr(0.075), hold: fr(0.035), open: fr(0.12),
      stagger: 0.0, depth: 1.0, repeat: false });
  };
  A.enableManualPreviewBlinks = function () {
    this.preview_blink_manual = true;
    this.blink_repeat = false;
    this.double_blink = false;
  };
  /** audition.rs 66-77 Gesture::perform: one of PipAnim.PREVIEW_GESTURES. Returns whether it is known. */
  A.performPreview = function (name) {
    switch (name) {
      case 'blink': this.previewBlink(); break;
      case 'gasp': this.previewFlash('surprise', 0.9); break;
      case 'poke': this.previewFlash('surprise', 0.7); this.previewBlink(); break;
      case 'startle-burst': this.previewStartleBurst(); break;
      default:
        if (PREVIEW_DURATION[name] == null) return false;
        this.preview_gesture = [name, this.t];
    }
    return true;
  };

  // anim.rs 823-851
  A.activity = function () {
    if (this.panel_blank) return 'blank';
    var gazeMoving = fr(this.t - this.gaze.start) < this.gaze.duration;
    var busy = this.lid_wake != null || this.birth != null || this.blink != null || this.gesture != null ||
      this.preview_gesture != null || !same(this.preview_gaze, [0.0, 0.0]) ||
      !this.spring.settled || this.level > 0.0 || this._touch != null || gazeMoving ||
      Math.abs(this.head_v) > 0.002 ||
      (this.isAudition(this.acting) && this.motion(this.acting).kick !== 'none' && this.t - this.acted_at < 0.86) ||
      (this.engaged() && !this.muted);
    if (busy) return 'active';
    if (this.asleep()) return 'asleep';
    return 'calm';
  };
  // anim.rs 854-856
  A.trialIdle = function () {
    return this.state === 'idle' && this.emotion == null && !this.dazed() && this.activity() === 'calm';
  };

  // anim.rs 858-875: the speech-end reply ladder experiment.
  A.enableReplyLadder = function () { this.reply_ladder = true; };
  A.acknowledgeSpeech = function (ageMs) {
    if (!this.reply_ladder || ageMs >= 2000 || this.state !== 'thinking' ||
      this.panel_blank || this.muted || this.dazed() || this.speech_end != null) return;
    this.speech_end = this.t - ageMs / 1000.0;
    this.thought_spot = [fr(fr(0.57) * this.thought_side), fr(0.44)];
    if (ageMs <= 250 && this.blink == null) this.blink_next = this.t;
  };

  // anim.rs 881-972
  A.setStateFull = function (state, muted, unwell) { this.setStateAtSpeechAge(state, muted, unwell, null); };
  /** ageMs: ms since the heard utterance ended (state.changed speech_end_mono_ms), or null. */
  A.setStateAtSpeechAge = function (state, muted, unwell, ageMs) {
    muted = !!muted; unwell = !!unwell;
    if (ageMs === undefined) ageMs = null;
    this.preview_dream = false;
    var changed = state !== this.state;
    var heard = this.state === 'listening' && state === 'thinking';
    if (changed) {
      this.speech_end = null;
      if (state === 'waking' && this.asleep()) this.omega = 2.5;
      if (this.state === 'speaking') this.endReply();
      if (state === 'speaking') this.speech_ended = false;
      if (state === 'attending' || state === 'listening' || state === 'speaking') this.screen_concern_until = 0.0;
      if (state === 'asleep') {
        this._touch = null;
        this.presence = null;
        this.emotion = null;
        this.blink_repeat = false;
        this.double_blink = false;
        if (this.blink) this.blink.repeat = false;
      }
      if (state === 'thinking') this.thought_side = this.rng.next() < 0.5 ? -1.0 : 1.0;
      if (state === 'listening') this.listen_side = this.rng.next() < 0.5 ? -1.0 : 1.0;
      var yawning = this.gesture != null && this.gesture[0] === 'yawn';
      if (!(yawning && state === 'asleep')) this.gesture = null;
      if (state === 'drowsy' && !muted && !unwell && !this.link_lost && !this.panel_blank) this.startGesture('yawn');
      this.reunion = 0.0;
      var s = this.state;
      var fromRest = s === 'idle' || s === 'drowsy' || s === 'waking' || s === 'asleep';
      if (state === 'attending' && fromRest && !muted && !unwell) {
        var eager = eagerness(this.drives);
        if (eager > 0.0 && this.t - this.reunited_at >= REUNION_GAP) {
          this.reunion = eager;
          this.reunited_at = this.t;
        }
      }
      if ((state === 'attending' || state === 'listening' || state === 'speaking') && this.birth) {
        if (this.birth.cut == null) this.birth.cut = this.t;
      }
      if (state === 'idle') this.gesture_next = this.t + this.rng.range(12.0, 24.0);
      this.quiet_since = this.t;
      this.yawned = false;
      this.state = state;
      this.state_at = this.t;
    }
    var statusChanged = muted !== this.muted || unwell !== this.unwell;
    this.muted = muted;
    this.unwell = unwell;
    if (muted || unwell || this.asleep()) {
      this.endReply();
      this.speech_end = null;
      this.screen_concern_until = 0.0;
      this.unheard_until = 0.0;
    }
    if (heard && this.layersVisible() && !(this.reply_ladder && ageMs != null)) this.startGesture('acknowledge');
    if (changed || statusChanged) {
      this.resetFixation();
      if (this.engaged()) {
        this.blink_next = Math.max(this.blink_next, this.t + 0.9);
        this.blink_repeat = false;
      }
    }
    if (changed && ageMs != null) this.acknowledgeSpeech(ageMs);
  };
  /** setState(name[, muted, unwell]): muted and unwell default to their current values. */
  A.setState = function (state, muted, unwell) {
    this.setStateFull(state, muted === undefined ? this.muted : muted, unwell === undefined ? this.unwell : unwell);
  };
  A.setMuted = function (on) { this.setStateFull(this.state, !!on, this.unwell); };
  A.setUnwell = function (on) { this.setStateFull(this.state, this.muted, !!on); };

  A.engaged = function () {
    var s = this.state;
    return s === 'attending' || s === 'listening' || s === 'speaking';
  };
  A.followsPresence = function () {
    return !this.muted && !this.unwell && !this.link_lost && (this.engaged() || this.state === 'idle');
  };
  // anim.rs 982-992
  A.resetFixation = function () {
    this.gaze_away = false;
    this.surveying = false;
    this.gaze_next = this.t;
    if (this.state === 'idle' && this.presence == null) {
      this.fixation = center();
      this.gaze_next += this.rng.range(2.4, 4.5);
    } else if (this.state === 'thinking' && !this.muted && !this.dazed()) {
      this.gaze_next += 0.12;
    }
  };

  // anim.rs 994-1021
  A.express = function (name, hold) {
    if (hold === undefined) hold = 2.5; // link.rs: face.express hold defaults to 2.5 s
    if (NAMES.indexOf(name) < 0 || name === 'dreaming') return;
    this.audition(name, 1.0, hold);
  };
  /** A typed pose at a strength measured from neutral (0..1.5), for hold seconds. */
  A.audition = function (name, intensity, hold) { this.auditionBlend(name, intensity, hold, null); };
  /** blend: null or [otherName, weight]. */
  A.auditionBlend = function (name, intensity, hold, blend) {
    if (!this.info[name] || (blend && !this.info[blend[0]])) return;
    if (this.panel_blank || this.asleep() || !isFinite(intensity) || !isFinite(hold)) return;
    this.emotion = { id: name, until: this.t + clamp(hold, 0.2, 600.0), intensity: fr(clamp(intensity, 0.0, 1.5)),
      blend: blend ? [blend[0], fr(blend[1])] : null };
    this.quiet_since = this.t;
    if (this.gesture != null && this.gesture[0] === 'content') this.gesture = null;
  };
  A.previewDreaming = function () {
    this.setStateFull('asleep', false, false);
    this.preview_dream = true;
    this.state_at = this.t;
  };
  // anim.rs 1030-1045
  A.setLowBattery = function (low) {
    if (low) {
      if (!this.battery_cue) this.battery_cue = { next: this.t, until: this.t };
    } else {
      this.battery_cue = null;
    }
  };
  A.endReply = function () {
    this.speech_cue = null;
    this.speech_ended = true;
    if (this.state === 'thinking' || this.state === 'speaking') {
      this.emotion = null;
      this.level_target = 0.0;
    }
  };
  // anim.rs 1047-1084
  A.setGaze = function (x, y) {
    if (this.panel_blank || this.asleep()) return;
    var measured = [clamp(x, -1.0, 1.0), clamp(y, -1.0, 1.0)];
    var point = measured;
    if (this.presence && distance(this.presence[0], measured) < 0.08) point = this.presence[0];
    if (this.followsPresence() && (this.presence == null || !same(this.presence[0], point))) this.gaze_next = this.t;
    var arriving = this.presence == null && this.state === 'idle' && this.followsPresence();
    if (arriving && eagerness(this.drives) > 0.5 && this.t - this.reunited_at >= REUNION_GAP) {
      this.reunited_at = this.t;
      this.express('joy', 1.2);
    }
    this.presence = [point, this.t + 2.0];
    this.quiet_since = this.t;
  };
  A.clearGaze = function () {
    if (this.presence) {
      this.presence = null;
      this.fixation = center();
      this.resetFixation();
    }
  };
  A.glance = function (x, y, hold) {
    if (hold === undefined) hold = 0.75;
    if (!this.panel_blank && !this.asleep()) {
      this._touch = [[clamp(x, -1.0, 1.0), clamp(y, -1.0, 1.0)], this.t + clamp(hold, 0.1, 2.0)];
      this.quiet_since = this.t;
    }
  };
  /** A touch on the screen: the Rust touch is a glance with a hold. */
  A.touch = function (x, y, hold) { this.glance(x || 0, y || 0, hold); };

  // anim.rs 1088-1147. closed: true / false / null (unknown).
  A.setLid = function (closed, showFace, snapshot) {
    var open = closed === false;
    var wasBlank = this.panel_blank;
    this.panel_blank = !(open || (closed == null && !!showFace));
    if (wasBlank && !this.panel_blank) this.lit_at = this.t;
    if (this.panel_blank) {
      this.endReply();
      this.screen_concern_until = 0.0;
      this.unheard_until = 0.0;
      if (!wasBlank) this.spring = new Spring(this, 'asleep');
      this.lid_wake = null;
      this.level = 0.0;
      this.level_target = 0.0;
      this.emotion = null;
      this.blink = null;
      this.blink_repeat = false;
      this.double_blink = false;
      if (this.battery_cue) this.battery_cue.until = this.t;
      this._touch = null;
      this.presence = null;
      this.fixation = center();
      this.gaze = new Gaze();
      this.gesture = null;
      this.heaviness = 0.0;
      this.head = 0.0;
      this.head_v = 0.0;
      if (this.birth) {
        var b = this.birth;
        this.birth = null;
        if (b.cut == null) this.birth_pending = this.t;
      }
      this.resetFixation();
    } else if (this.birth_pending != null && this.t - this.birth_pending <= BIRTH_PENDING) {
      this.lit_at = this.t;
      this.startBirth(0.0);
    } else if (open && !this.lid_open && !snapshot) {
      this.quiet_since = this.t;
      this.lid_wake = { started: this.t, dreamed: false };
      this.blink = null;
      this.blink_repeat = false;
      this.blink_next = this.t + 2.8;
    }
    this.lid_open = open;
    return !this.panel_blank;
  };
  A.dreamCompletedOnOpen = function () {
    if (this.lid_wake && this.lid_wake.started === this.t) this.lid_wake.dreamed = true;
  };

  /**
   * The lid wake (1.23 s): closed line, peek, blink, perk up, settle into
   * the live face. opts.state (default "idle") is set first, as the solo
   * demo does; opts.dreamed adds the dream flutter to the waking blink.
   */
  A.wake = function (opts) {
    opts = opts || {};
    if (this.lid_open) this.setLid(true, false, false);
    this.setState(opts.state || 'idle');
    this.setLid(false, false, false);
    if (opts.dreamed) this.dreamCompletedOnOpen();
  };
  A.sleep = function () { this.setState('asleep'); };

  // anim.rs 1150-1191
  A.perform = function (name) {
    var g;
    if (name === 'content' || name === 'yawn' || name === 'nod' || name === 'shake') g = name;
    else if (name === 'survey') {
      if (!this.panel_blank && this.state === 'idle' && this.presence == null) this.survey();
      return true;
    } else return false;
    var headGesture = g === 'nod' || g === 'shake';
    if (!this.panel_blank && !this.asleep() &&
      (!headGesture || (!this.muted && !this.dazed() && this.lid_wake == null && this.birth == null))) {
      this.startGesture(g);
    }
    return true;
  };
  A.startGesture = function (g) {
    var side = this.rng.next() < 0.5 ? -1.0 : 1.0;
    this.gesture = [g, this.t, side];
    var end = this.t + GESTURE_LENGTH[g];
    this.blink_next = Math.max(this.blink_next, end + this.rng.range(0.3, 1.2));
  };
  A.survey = function () {
    var side = this.rng.next() < 0.5 ? -1.0 : 1.0;
    this.gaze_away = true;
    this.surveying = true;
    var fx = side * this.rng.range(0.72, 0.95);
    this.fixation = [fx, this.rng.range(-0.08, 0.18)];
    this.gaze_next = this.t + this.rng.range(1.8, 2.8);
    if (this.blink == null && this.gesture == null) this.blink_next = Math.min(this.blink_next, this.t + 0.06);
  };

  A.panelBlank = function () { return this.panel_blank; };
  // anim.rs 1197-1214
  A.setLevel = function (v) {
    if (this.panel_blank || !isFinite(v)) return;
    if (this.state === 'speaking' && this.level_target > 0.35 && v <= 0.06 &&
      this.blink == null && this.blink_next - this.t < 0.75) {
      this.blink_next = Math.min(this.blink_next, this.t + 0.16);
    }
    this.level_target = Math.sqrt(clamp((v - 0.06) / 0.94, 0.0, 1.0));
    this.level_at = this.t;
    if (this.level_target > 0.0) this.quiet_since = this.t;
  };
  // anim.rs 1216-1238: say.level with its face_cue.
  var SPEECH_CUES = { concern: 1, joy: 1, curiosity: 1, surprise: 1, skepticism: 1, sleepy: 1 };
  /** cue: null, 'neutral' or one of concern, joy, curiosity, surprise, skepticism, sleepy. */
  A.speechLevel = function (level, cue) {
    if (this.state === 'speaking' && this.speech_ended) return;
    this.setLevel(level);
    if (this.state !== 'speaking' || this.panel_blank || this.muted || this.dazed() ||
      !isFinite(level) || level < fr(0.015)) return;
    var expression;
    if (cue === 'neutral') expression = null;
    else if (cue != null && SPEECH_CUES[cue] === 1) expression = cue;
    else return;
    this.emotion = null;
    this.speech_cue = expression;
  };
  // anim.rs 1241-1253
  A.screenOutcome = function (success) {
    if (!this.layersVisible() || !this.followsFocus()) return;
    if (success) {
      this.screen_concern_until = 0.0;
      this.glance(0.0, fr(-0.65), 0.50);
      this.startGesture('acknowledge');
    } else {
      this.glance(0.0, fr(0.12), 0.72);
      this.screen_concern_until = this.t + 0.72;
    }
  };
  A.setLinkLost = function (lost) {
    lost = !!lost;
    if (this.link_lost !== lost) {
      this.link_lost = lost;
      if (lost) {
        this.endReply();
        this.screen_concern_until = 0.0;
        this.unheard_until = 0.0;
        this.setBreathEffort(null);
      }
      this.resetFixation();
    }
  };
  A.asleep = function () { return this.state === 'asleep'; };
  A.isMuted = function () { return this.muted; };
  // anim.rs 1276-1279
  A.layersVisible = function () {
    return !this.panel_blank && !this.muted && !this.dazed() && this.lid_wake == null &&
      this.birth == null && !this.loading && this.state !== 'waking';
  };
  /**
   * anim.rs 1281-1302 decor_motion(false): what the brow and mouth layers
   * follow, or null when they stay hidden. {expression, blend, strength,
   * speech, time, reflected}.
   */
  A.decorMotion = function () {
    var expression = this.targetName();
    var speaking = this.state === 'speaking' && !this.speech_ended;
    if (!this.layersVisible() || this.battery_cue != null || this.state === 'drowsy' ||
      (NO_DECOR[expression] === 1 && !speaking)) return null;
    return { expression: expression, blend: this.blendOf(expression), strength: this.intensity(expression),
      speech: this.state === 'speaking' ? this.level : 0.0, time: this.t, reflected: this.facingLeft() };
  };
  /**
   * anim.rs 1304-1339 emote_motion(false): the live status emotes, or null.
   * {question, dots, battery, burst, age}: question after unclear words,
   * thinking dots, the low-battery mark.
   */
  A.emoteMotion = function () {
    if (!this.layersVisible()) return null;
    var age = Math.max(this.t - this.acted_at, 0.0);
    var fx = { question: 0.0, dots: 0.0, battery: 0.0, burst: 0.0, age: age };
    if ((this.state === 'attending' || this.state === 'listening') && this.t < this.unheard_until) {
      fx.question = 0.68 * ease(this.unheard_until - this.t, 0.0, 0.25);
    } else if (this.state === 'thinking') {
      fx.dots = 0.8;
    } else if (this.state === 'idle' && this.targetName() === 'tired' && this.battery_cue && this.t < this.battery_cue.until) {
      fx.battery = 0.8;
    } else {
      return null;
    }
    return fx;
  };
  A.resting = function () {
    var s = this.state;
    return !this.panel_blank && !this.muted && !this.dazed() && (s === 'idle' || s === 'drowsy' || s === 'asleep');
  };
  A.missed = function () {
    if (!this.muted && !this.dazed()) this.express('skepticism', MISSED_HOLD);
  };
  // anim.rs 1356-1360: hear.missed with reason "unclear" (the question emote).
  A.unclearHearing = function () {
    if (this.layersVisible() && (this.state === 'attending' || this.state === 'listening')) {
      this.unheard_until = this.t + MISSED_HOLD;
    }
  };
  A.dazed = function () { return this.unwell || this.link_lost; };

  // anim.rs 1371-1406: the override order.
  A.targetName = function () {
    if (this.asleep()) return this.preview_dream ? 'dreaming' : 'asleep';
    if (this.dazed()) return this.loading && !this.unwell ? 'loading' : 'dazed';
    if (this.muted) return 'muted';
    if (this.preview_flash && this.t < this.preview_flash[1]) return this.preview_flash[0];
    if (this.emotion && this.t < this.emotion.until) return this.emotion.id;
    if (this.state === 'speaking') return this.speech_cue || 'speaking';
    if (this.followsFocus() && this.t < this.screen_concern_until) return 'concern';
    if (this.state === 'idle' && this.battery_cue && this.t < this.battery_cue.until) return 'tired';
    switch (this.state) {
      case 'attending': return 'attentive';
      case 'listening': return 'listening';
      case 'thinking': return 'thinking';
      case 'speaking': return 'speaking';
      case 'drowsy': return 'sleepy';
      default: return 'neutral';
    }
  };
  A.shapeName = function () {
    var name = this.targetName();
    var stretching = this.gesture != null && this.gesture[0] === 'yawn' && fr(this.t - this.gesture[1]) < YAWN_STRETCH;
    return stretching && (name === 'sleepy' || name === 'asleep') ? 'neutral' : name;
  };
  // anim.rs 1421-1442
  A.held = function (name) {
    if (this.preview_flash && this.t < this.preview_flash[1]) return null;
    var e = this.emotion;
    return e && e.id === name && this.t < e.until ? e : null;
  };
  A.intensity = function (name) {
    var held = this.held(name);
    if (held) return held.intensity;
    if (this.state === 'speaking' && this.speech_cue === name) return fr(0.55);
    if (name === 'concern' && this.followsFocus() && this.t < this.screen_concern_until) return fr(0.52);
    return 1.0;
  };
  A.blendOf = function (name) {
    var held = this.held(name);
    return held ? held.blend : null;
  };
  A.facingLeft = function () {
    if (this.shapeName() !== 'thinking') return false;
    var p = this.presets.thinking;
    return (p[0] + p[N]) * this.thought_side < -0.01;
  };
  A.blinkInterval = function () {
    return mixf(1.0, this.motion(this.acting).blink_interval, this.acting_intensity);
  };

  A.stepSpring = function (dt) {
    this.spring.damping = this.info[this.targetName()].damping;
    this.spring.flip = this.facingLeft();
    var shape = this.shapeName();
    this.spring.step(shape, dt, this.omega, this.intensity(shape), this.blendOf(shape));
  };

  /** Rust Anim::frame (anim.rs 1470-1708): returns the pose {l, r}. */
  A.step = function (elapsed, receive) {
    elapsed = Math.max(elapsed, 0.0);
    var dt = Math.min(elapsed, 0.1);
    var previous = this.t;
    var now = previous + elapsed;
    if (!this.panel_blank) {
      var s = this.state;
      var tempo = (s === 'attending' || s === 'listening') ? 20.0 : (s === 'drowsy' || s === 'asleep') ? 8.0 : 14.0;
      var speed = s === 'waking' ? 0.8 : 4.5;
      this.omega = fr(this.omega + fr(fr(tempo - this.omega) * Math.min(fr(fr(dt) * speed), 1.0)));
      var held = this.emotion ? clamp(fr(this.emotion.until - previous), 0.0, fr(elapsed)) : fr(elapsed);
      this.stepSpring(held);
      if (held < elapsed) {
        this.emotion = null;
        this.stepSpring(fr(fr(elapsed) - held));
      }
      this.stepVoice(elapsed);
    }
    this.t = now;
    if (receive) receive(this);

    // anim.rs 1500-1525: notice a new expression (or strength) arriving.
    var arriving = this.targetName();
    var intensity = this.intensity(arriving);
    if (arriving !== this.acting || intensity !== this.acting_intensity) {
      var oldInterval = this.blinkInterval();
      var newExpression = arriving !== this.acting;
      this.acted_from = this.acting;
      this.acting = arriving;
      this.acting_intensity = intensity;
      if (newExpression) this.acted_at = this.t;
      var interval = this.blinkInterval();
      if (interval !== oldInterval && !this.blink_repeat && !this.double_blink && this.blink_next > this.t) {
        this.blink_next = this.t + (this.blink_next - this.t) * fr(interval / oldInterval);
      }
      if (newExpression && this.motion(arriving).kick === 'startle' && intensity >= fr(0.55) &&
        !this.panel_blank && this.lid_wake == null) {
        this.double_blink = true;
        if (this.blink == null) this.blink_next = this.t + 0.30;
      }
    }

    var p = poseFrom(this.spring.x);
    if (this.panel_blank) {
      p.l.glow = 0.0;
      p.r.glow = 0.0;
      return p;
    }

    var asleep = this.asleep();
    var t = this.t;
    var gaze = this.stepGaze();
    // anim.rs 1537-1551: a Face Lab preview gesture's movement and gaze.
    var movement = this.preview_gesture ? previewMotion(this.preview_gesture[0], t - this.preview_gesture[1]) : null;
    if (!movement) this.preview_gesture = null;
    if (movement || !same(this.preview_gaze, [0.0, 0.0])) {
      var target = movement ? movement.gaze : [0.0, 0.0];
      var stepAmount = 1.0 - Math.exp(-16.0 * dt);
      for (var k = 0; k < 2; k++) {
        var offset = mix(this.preview_gaze[k], target[k], stepAmount);
        if (target[k] === 0.0 && Math.abs(offset) < 0.001) offset = 0.0;
        this.preview_gaze[k] = offset;
        gaze[k] = clamp(gaze[k] + offset, -1.2, 1.2);
      }
    }
    var wake = null;
    if (this.lid_wake && fr(this.t - this.lid_wake.started) < WAKE_DURATION) {
      wake = [this.t - this.lid_wake.started, this.lid_wake.dreamed];
    }
    if (!wake) this.lid_wake = null;
    var birth = null;
    if (this.birth) {
      var age0 = this.t - this.birth.started;
      var cut0 = this.birth.cut == null ? null : this.t - this.birth.cut;
      if (fr(age0) < BIRTH && (cut0 == null || fr(cut0) < BIRTH_HANDOVER)) birth = [age0, cut0];
    }
    if (!birth) this.birth = null;
    if (!wake && !birth) this.stepBlink();
    if (this.lid_open && !wake && this.state === 'idle' && !this.muted && !this.dazed() && this.emotion == null) {
      var cue = this.battery_cue;
      if (cue && this.t >= cue.next) {
        cue.until = this.t + 6.0;
        cue.next = this.t + 45.0;
      }
    }
    // anim.rs 1586-1599
    var depth = asleep ? 1.0 : 0.0;
    this.breath_depth += (depth - this.breath_depth) * dt;
    if (this.breath_experiment) {
      var effort = asleep ? 0.0 : this.breath_effort_target;
      this.breath_effort += (effort - this.breath_effort) * dt;
    }
    var rate = fr(mix(0.22, 0.13, this.breath_depth) + 0.07 * this.breath_effort);
    this.breath_phase = (this.breath_phase + TAU * rate * elapsed) % TAU;
    var breath = Math.sin(this.breath_phase);
    var restless = asleep && this.dazed() && !this.loading;
    if (restless) breath *= 0.65 + 0.35 * Math.sin(t * 0.37);

    // anim.rs 1601-1616: the arriving expression's gaze bias and breath.
    var expressive = !asleep && !this.muted && !this.dazed();
    var motion = this.motion(this.acting);
    var strength = expressive && !wake && !birth ? this.acting_intensity : 0.0;
    var smooth = 1.0 - Math.exp(-dt * 9.0);
    var freeGaze = this._touch == null && this.presence == null && !(this.focus != null && this.followsFocus());
    for (var ax = 0; ax < 2; ax++) {
      this.gaze_bias[ax] += (motion.gaze[ax] * (freeGaze ? strength : 0.0) - this.gaze_bias[ax]) * smooth;
      if (freeGaze && Math.abs(this.gaze_bias[ax]) > 0.0001) gaze[ax] = clamp(gaze[ax] + this.gaze_bias[ax], -1.0, 1.0);
    }
    var breathing = mix(1.0, motion.breath, strength);
    this.breath_scale += (breathing - this.breath_scale) * smooth;
    breath *= this.breath_scale;

    var life = this.stepLife(elapsed, dt, expressive, wake == null);
    var engaged = this.engaged() && expressive;
    var speech = expressive ? this.level : 0.0;
    var age = this.t - this.state_at;
    var drift = [0.006 * Math.sin(t * 0.081), 0.004 * Math.sin(t * 0.107 + 1.0)];
    var wave = function (r, phase) { return Math.sin(r * t + phase); };
    var roll = movement ? movement.roll : 0.0;
    var guarded = this.spring.guarded;
    var eyes = [[-1.0, p.l], [1.0, p.r]];
    for (var i = 0; i < 2; i++) {
      var side = eyes[i][0], e = eyes[i][1];
      e.h *= 1.0 + mix(0.011, 0.045, this.breath_depth) * breath;
      e.y += mix(0.003, 0.012, this.breath_depth) * breath;
      e.x += gaze[0] * 0.137 + drift[0];
      e.y += gaze[1] * 0.083 + drift[1];
      var near = 1.0 + 0.045 * side * gaze[0];
      e.w *= near;
      e.h *= near;
      e.tilt += 0.038 * gaze[0];

      if (engaged) {
        var focusAge = age - (side + 1.0) * 0.012;
        var focus = ease(focusAge, 0.0, 0.10) * (1.0 - ease(focusAge, 0.12, 0.38));
        e.h *= 1.0 + 0.048 * focus;
        e.y += 0.013 * focus;
      }
      var accent = speech * (1.0 - 0.11 * side);
      e.h *= 1.0 + 0.086 * accent;
      e.y += 0.012 * accent;
      e.glow *= 1.0 + 0.105 * speech;
      applyLife(life, e, side, this.heaviness, this.head + roll);
      if (movement) {
        e.x += movement.x;
        e.y += movement.y;
      }

      if (!asleep && this.dazed()) {
        if (this.loading && !this.unwell) {
          e.glow *= 0.88 + 0.12 * wave(1.6, 0.0);
        } else {
          e.tilt += 0.038 * wave(1.65, side);
          e.glow *= 0.94 + 0.055 * wave(2.2, 2.0 * side);
        }
      }
      if (restless) {
        e.tilt += 0.05 * side + 0.02 * wave(0.29, side);
        e.y += 0.010 * side;
        e.top += 0.05 * (side + 1.0) * 0.5;
        e.glow *= 0.92 + 0.06 * wave(0.53, 1.0 + side);
      }
      if (this.preview_dream) {
        var dage = age - (side + 1.0) * 0.055;
        dreamFlutter(e, ease(dage, 0.42, 0.62) * (1.0 - ease(dage, 0.64, 1.05)));
      }
      e.h = Math.max(e.h, 0.012);
      if (this.blink) closeLids(e, this.blink.closure(this.t, side));
      if (guarded) {
        // anim.rs 1685-1700: keep a guarded pose's oriented eye in the safe area.
        e.w = clamp(e.w, 0.012, 1.45);
        e.h = clamp(e.h, 0.012, 0.85);
        e.round = Math.max(e.round, 2.0);
        e.glow = clamp(e.glow, 0.0, 1.3);
        var sn = Math.sin(e.tilt), cs = Math.cos(e.tilt);
        var bulge = (Math.pow(2.0, 0.5 - 1.0 / e.round) - 1.0) * Math.min(e.w, e.h);
        var reachX = Math.hypot(e.w * cs, e.h * sn) + bulge;
        var reachY = Math.hypot(e.w * sn, e.h * cs) + bulge;
        var edgeX = Math.max(1.499 - Math.max(e.w, reachX), 0.0);
        var edgeY = Math.max(0.899 - Math.max(e.h, reachY), 0.0);
        e.x = clamp(e.x, -edgeX, edgeX);
        e.y = clamp(e.y, -edgeY, edgeY);
      }
    }
    if (wake) p = lidWakePose(this, wake[0], p, wake[1]);
    if (birth) {
      if (birth[1] == null) return birthPose(this, birth[0], p);
      return blendPose(birthPose(this, birth[0], p), p, ease(birth[1], 0.0, BIRTH_HANDOVER));
    }
    return p;
  };

  // anim.rs 1712-1842
  A.stepLife = function (elapsed, dt, expressive, awake) {
    if (this.gesture != null && fr(this.t - this.gesture[1]) >= GESTURE_LENGTH[this.gesture[0]]) this.gesture = null;
    var composed = !this.muted && !this.dazed();
    if (!composed) this.gesture = null;

    var alone = this.state === 'idle' && expressive && awake && this.emotion == null;
    var heavy = 0.0;
    if (alone && this.presence == null && this._touch == null) {
      var quiet = 0.42 * ease(this.t - this.quiet_since, 150.0, 480.0);
      heavy = Math.min(quiet + tiredness(this.drives), 0.42);
    }
    var timeConstant = heavy > this.heaviness ? 4.0 : 0.35;
    this.heaviness += (heavy - this.heaviness) * (1.0 - Math.exp(-elapsed / timeConstant));
    if (heavy === 0.0 && this.heaviness < 0.05) this.yawned = false;
    if (alone && this.gesture == null && this.blink == null && !this.typing_hidden && this.focus == null) {
      if (this.heaviness > 0.3 && !this.yawned) {
        this.yawned = true;
        this.startGesture('yawn');
      } else if (this.t >= this.gesture_next) {
        this.gesture_next = this.t + this.rng.range(20.0, 38.0);
        if (this.heaviness < 0.2 && (this.battery_cue == null || this.t >= this.battery_cue.until)) {
          this.startGesture('content');
        }
      }
    }

    var kind = null, gAge = 0.0, gSide = 0.0;
    if (this.gesture) { kind = this.gesture[0]; gAge = this.t - this.gesture[1]; gSide = this.gesture[2]; }
    var content = kind === 'content' ? ease(gAge, 0.0, 0.45) * (1.0 - ease(gAge, 1.35, 2.2)) : 0.0;
    var inhale = 0.0, squeeze = 0.0;
    if (kind === 'yawn') {
      inhale = ease(gAge, 0.0, 0.45) * (1.0 - ease(gAge, 0.55, 0.85));
      squeeze = ease(gAge, 0.6, 0.95) * (1.0 - ease(gAge, 1.5, 2.3));
    }

    var ai = this.acting_intensity;
    var roll = 0.0;
    if (expressive) {
      if (this.acting === 'curiosity') roll = -0.075 * ai;
      else if (this.acting === 'listening') roll = 0.028 * this.listen_side * ai;
      else if (this.acting === 'thinking') roll = 0.04 * this.thought_side * ai;
      else roll = this.motion(this.acting).roll * ai;
    }
    var age = this.t - this.acted_at;
    roll += gSide * (0.035 * content + 0.05 * squeeze);
    var steps = Math.max(Math.ceil(fr(fr(dt) / fr(0.02))), 1.0);
    var h = dt / steps;
    for (var i = 0; i < steps; i++) {
      this.head_v += (70.0 * (roll - this.head) - 13.4 * this.head_v) * h;
      this.head += this.head_v * h;
    }

    var kick = this.motion(this.acting).kick;
    var strength = expressive ? ai : 0.0;
    var tuck = expressive && this.info[this.acting].lively ? anticipation(age) * (kick === 'startle' ? 1.6 : 1.0) * strength : 0.0;
    var bounce = 0.0;
    if (expressive) {
      if (this.acting === 'joy') bounce = 0.042 * hop(age - 0.07, 0.24, 2.0);
      else if (this.acting === 'attentive' &&
        (this.acted_from === 'neutral' || this.acted_from === 'sleepy' || this.acted_from === 'tired')) {
        bounce = 0.036 * (1.0 + 1.2 * this.reunion) * hop(age - 0.04, 0.26, 1.0);
      } else if (kick === 'laugh') bounce = 0.055 * hop(age - 0.07, 0.215, 2.0);
      else if (kick === 'relief') bounce = -0.026 * pulse(age, 0.12, 0.48, 0.86);
    }
    bounce *= strength;
    var pop = (kick === 'startle' ? ease(age, 0.04, 0.11) * (1.0 - ease(age, 0.13, 0.45)) : 0.0) * strength;
    var recoil = (kick === 'startle' ? 0.9 * pulse(age, 0.14, 0.24, 0.55) :
      kick === 'flinch' ? 0.72 * pulse(age, 0.03, 0.15, 0.48) : 0.0) * strength;
    var nod = 0.0;
    if (expressive && kind === 'nod') {
      nod = 0.024 * pulse(gAge, 0.0, 0.10, 0.23) - 0.089 * pulse(gAge, 0.10, 0.32, 0.68);
    } else if (expressive && kind === 'acknowledge') {
      nod = 0.020 * pulse(gAge, 0.0, 0.10, 0.20) - 0.024 * pulse(gAge, 0.14, 0.27, 0.55);
    }
    var shake = 0.0;
    if (expressive && kind === 'shake') {
      shake = gSide * (pulse(gAge, 0.0, 0.18, 0.38) - 0.95 * pulse(gAge, 0.18, 0.46, 0.72) +
        0.32 * pulse(gAge, 0.46, 0.68, 0.82));
    }
    var smile = content > 0.0 ? this.pose('joy') : null;
    return { tuck: tuck, bounce: bounce, pop: pop, recoil: recoil, nod: nod, shake: shake, content: content,
      inhale: inhale, squeeze: squeeze, smile: smile };
  };

  // anim.rs 1844-1859
  A.stepVoice = function (elapsed) {
    var audible = this.state === 'speaking' && !this.muted && !this.dazed() && this.level_target > 0.0;
    var voiced = audible ? clamp(this.level_at + 0.20 - this.t, 0.0, elapsed) : 0.0;
    if (voiced > 0.0 && this.level !== this.level_target) {
      var tc = this.level_target > this.level ? 0.044 : 0.15;
      this.level = mix(this.level, this.level_target, 1.0 - Math.exp(-voiced / tc));
    }
    if (this.level > 0.0 && elapsed > voiced) {
      this.level *= Math.exp(-(elapsed - voiced) / 0.15);
      if (this.level < 0.0005) this.level = 0.0;
    }
  };

  // anim.rs 1861-1868: the owner's idle-glances preference.
  A.setIdleGlances = function (enabled) {
    enabled = !!enabled;
    if (this.idle_glances !== enabled) {
      this.idle_glances = enabled;
      if (this.state === 'idle' && this.presence == null) this.gaze_next = this.t;
    }
  };

  // anim.rs 1870-1976
  A.stepGaze = function () {
    if (this.presence && this.t >= this.presence[1]) {
      this.presence = null;
      if (this.followsPresence()) this.resetFixation();
    }
    if (this.focus && this.followsFocus()) return this.stepFocus(this.focus);

    // The reply ladder: hold on the speaker, look off to think, search, settle.
    if (this.speech_end != null) {
      var ladderAge = this.t - this.speech_end;
      if (ladderAge >= 2.0 || this.state !== 'thinking' || this.muted || this.dazed()) {
        this.speech_end = null;
        this.gaze_next = this.t;
      } else if (this._touch == null) {
        var who = this.presence ? this.presence[0] : center();
        var sx = this.thought_spot[0], sy = this.thought_spot[1], ts = this.thought_side;
        this.gaze_away = ladderAge >= 0.30;
        if (!this.gaze_away) this.fixation = who;
        else if (ladderAge < 1.0) this.fixation = [sx, sy];
        else {
          switch (Math.floor((ladderAge - 1.0) / 0.25)) {
            case 0: this.fixation = [fr(sx + fr(0.08) * ts), fr(sy + fr(0.06))]; break;
            case 1: this.fixation = [fr(sx - fr(0.06) * ts), sy]; break;
            case 2: this.fixation = [fr(sx + fr(0.07) * ts), fr(sy - fr(0.07))]; break;
            default: this.fixation = [sx, sy];
          }
        }
        return this.gaze.track(this.fixation, this.t, 0.0);
      }
    }

    if (this.t >= this.gaze_next) {
      var subject = this.presence ? this.presence[0] : center();
      var returning = this.surveying && this.gaze_away;
      this.surveying = false;
      var point, wait;
      if (this.asleep() || this.muted || this.dazed() || this.state === 'waking') {
        point = center(); wait = 1.0;
      } else if (this.engaged() || (this.state === 'idle' && this.presence)) {
        this.gaze_away = false;
        var cx = this.rng.range(-0.027, 0.027), cy = this.rng.range(-0.018, 0.018);
        wait = this.state === 'listening' ? this.rng.range(2.2, 3.8) : this.rng.range(0.9, 2.0);
        point = [fr(subject[0] + cx), fr(subject[1] + cy)];
      } else if (this.state === 'drowsy') {
        point = [0.0, fr(-0.22)]; wait = 3.0;
      } else if (this.state === 'thinking') {
        this.gaze_away = !this.gaze_away;
        if (this.gaze_away) {
          var x = fr(this.thought_side * this.rng.range(0.50, 0.68));
          this.thought_spot = [x, this.rng.range(0.38, 0.56)];
          this.search_next = this.t + this.rng.range(0.55, 1.0);
          point = this.thought_spot; wait = this.rng.range(3.8, 6.4);
        } else {
          point = [fr(0.65 * subject[0]), fr(0.65 * subject[1])];
          wait = this.rng.range(0.55, 0.90);
        }
      } else if (this.state === 'idle' && !this.idle_glances) {
        this.gaze_away = false;
        point = center(); wait = 2.0;
      } else if (this.gaze_away) {
        this.gaze_away = false;
        this.surveying = returning;
        point = center(); wait = this.rng.range(2.5, 5.3);
      } else if (this.state === 'idle' && !this.typing_hidden && this.rng.next() < 0.28) {
        this.survey();
        point = this.fixation; wait = fr(this.gaze_next - this.t);
      } else {
        this.gaze_away = true;
        var side = this.rng.next() < 0.5 ? -1.0 : 1.0;
        var gx = fr(side * this.rng.range(0.24, 0.51));
        point = [gx, this.rng.range(-0.22, 0.29)];
        wait = this.rng.range(0.75, 1.55);
      }
      this.fixation = [clamp(point[0], -1.0, 1.0), clamp(point[1], -1.0, 1.0)];
      var wandering = this.state === 'idle' && this.presence == null && !this.surveying;
      var tempo = wandering ? fr(gazeTempo(this.drives)) : 1.0;
      this.gaze_next = this.t + fr(wait * tempo);
    } else if (this.state === 'thinking' && this.gaze_away && !this.muted && !this.dazed() &&
      this.t >= this.search_next && this.t + 0.35 < this.gaze_next) {
      var reach = fr(this.thought_side * this.rng.range(-0.07, 0.14));
      var spot = this.thought_spot;
      var fx = clamp(fr(spot[0] + reach), -1.0, 1.0);
      this.fixation = [fx, clamp(fr(spot[1] + this.rng.range(-0.07, 0.07)), -1.0, 1.0)];
      this.search_next = this.t + this.rng.range(0.4, 0.9);
    }

    if (this._touch && this.t >= this._touch[1]) this._touch = null;
    var unhurried = this.surveying && this._touch == null ? fr(0.62) : 0.0;
    return this.gaze.track(this._touch ? this._touch[0] : this.fixation, this.t, unhurried);
  };

  A.followsFocus = function () {
    var s = this.state;
    return !this.muted && !this.dazed() && !this.asleep() && (s === 'idle' || s === 'thinking');
  };
  // anim.rs 1984-2011
  A.stepFocus = function (focus) {
    this.surveying = false;
    this.gaze_away = false;
    this.gaze_next = this.t + 0.5;
    if (focus.kind === 'paused') {
      this.fixation = [0.0, fr(0.12)];
    } else {
      var target = [clamp(focus.at[0], -1.0, 1.0), clamp(focus.at[1], -1.0, 1.0)];
      if (distance(target, this.focus_seen) > 0.015) {
        var enter = target[1] < this.focus_seen[1] - 0.004 && target[0] < this.focus_seen[0] - 0.3;
        this.focus_seen = target;
        this.focus_next = this.t + this.rng.range(0.08, 0.16);
        if (enter && focus.kind === 'typing' && this.blink == null) this.blink_next = Math.min(this.blink_next, this.t + 0.12);
      }
      if (this.t >= this.focus_next) this.fixation = this.focus_seen;
    }
    if (this._touch && this.t >= this._touch[1]) this._touch = null;
    return this.gaze.track(this._touch ? this._touch[0] : this.fixation, this.t, 0.0);
  };

  // anim.rs 2017-2087
  A.stepBlink = function () {
    if (this.preview_blink_manual) {
      if (this.blink && this.blink.done(this.t)) this.blink = null;
      return;
    }
    if (this.asleep()) {
      if (this.blink && this.blink.done(this.t)) this.blink = null;
      this.blink_next = this.t + 2.0;
      return;
    }
    var tn = this.targetName();
    var drowsy = tn === 'sleepy' || tn === 'tired' || this.heaviness > 0.22;
    if (this.blink) {
      if (!this.blink.done(this.t)) return;
      this.blink_repeat = this.blink.repeat;
      this.blink = null;
      var wait;
      if (this.double_blink) wait = Math.max(fr(this.acted_at + 0.30 - this.t), fr(0.05));
      else if (this.blink_repeat) wait = this.rng.range(0.14, 0.23);
      else if (this.state === 'listening') wait = this.rng.range(3.8, 6.8);
      else if (this.focus && this.focus.kind === 'typing') wait = this.rng.range(4.0, 8.5);
      else wait = this.rng.range(2.5, 5.6);
      var interval = this.blink_repeat || this.double_blink ? 1.0 : this.blinkInterval();
      this.blink_next = this.t + fr(wait * interval);
    }
    if (this.t < this.blink_next) return;
    var double = this.double_blink;
    if (this.state === 'speaking' && this.level > 0.32 && this.t < this.blink_next + 0.42 && !this.blink_repeat && !double) return;
    this.double_blink = false;
    var secondary = this.blink_repeat;
    var startle = double || (secondary && this.startle_followup);
    this.startle_followup = double;
    var soft = !drowsy && !startle && this.rng.next() < 0.22;
    var depth = soft ? this.rng.range(0.55, 0.78) : 1.0;
    var pace = startle ? fr(0.8) : drowsy ? fr(1.55) : this.rng.range(0.87, 1.17);
    var stagger = this.rng.range(0.007, 0.018);
    var close = fr(fr(0.075) * pace), hold = fr(fr(0.035) * pace), open = fr(fr(0.120) * pace);
    var signed = this.rng.next() < 0.5 ? stagger : -stagger;
    var repeat = double || (!secondary && !soft && !drowsy && this.state !== 'listening' && this.rng.next() < 0.12);
    this.blink = new Blink({ start: this.t, close: close, hold: hold, open: open, stagger: signed, depth: depth, repeat: repeat });
    this.blink_repeat = false;
  };

  /**
   * One display frame. dt in seconds since the last frame; receive is an
   * optional callback run after time catches up (Rust's receive closure).
   * Returns {u: Float32Array(32) in Pose::uniforms layout, activity, hz, pose}.
   */
  A.frame = function (dt, receive) {
    var p = this.step(dt, receive);
    var activity = this.activity();
    return { u: uniforms(p, this._u), activity: activity, hz: hz(activity, this.software), pose: p };
  };

  var PipAnim = {
    /**
     * opts.presets: name -> 26 numbers (Pose::to_array): all NAMES plus
     *   "loading", and any audition poses (Shell Lab's FACE.presets).
     * opts.seed: u32 (same seed, same idle life as the Rust face). Random if absent.
     * opts.software: cap the frame rate at 20 Hz as for a software renderer.
     * opts.motion: optional name -> {gaze, roll, blink_interval, breath, kick}
     *   for concept-only auditions (see conceptMotion); overrides NATIVE.
     * opts.replyLadder: enable the speech-end reply ladder (being.face_reply_ladder).
     * opts.effortBreathing: enable body-effort breathing (being.body_breath).
     * opts.lid: "open" to start with the lid known open without a wake
     *   (Rust set_lid(Some(false), false, true)); default leaves it unknown,
     *   lit, as Anim::new does.
     */
    create: function (opts) {
      opts = opts || {};
      var seed = opts.seed == null ? Math.floor(Math.random() * 4294967296) : opts.seed;
      var a = new Anim(opts.presets, seed, opts.software, opts.motion);
      if (opts.replyLadder) a.enableReplyLadder();
      if (opts.effortBreathing) a.enableEffortBreathing();
      if (opts.lid === 'open') a.setLid(false, false, true);
      return a;
    },
    NAMES: NAMES.slice(),
    STATES: ['waking', 'idle', 'attending', 'listening', 'thinking', 'speaking', 'drowsy', 'asleep'],
    SPEECH_CUES: ['neutral', 'concern', 'joy', 'curiosity', 'surprise', 'skepticism', 'sleepy'],
    PREVIEW_GESTURES: PREVIEW_GESTURES.slice(),
    GESTURES: ['content', 'yawn', 'nod', 'shake', 'survey'],
    hz: hz,
    conceptMotion: conceptMotion,
    uniforms: function (p) { return uniforms(p); },
    closeLids: closeLids,
    poseArray: poseArray,
    poseFrom: poseFrom,
    // Internals exposed for tests and tooling.
    _internal: { Anim: Anim, Spring: Spring, Rng: Rng, Blink: Blink, closeLids: closeLids, applyLife: applyLife,
      reflect: reflect, ease: ease, pulse: pulse, blinkClosure: blinkClosure, previewMotion: previewMotion,
      blendPose: blendPose, NATIVE: NATIVE, BIRTH: BIRTH, BIRTH_HANDOVER: BIRTH_HANDOVER,
      MISSED_HOLD: MISSED_HOLD, WAKE_DURATION: WAKE_DURATION, DEFAULT_DAMPING: DEFAULT_DAMPING },
  };

  root.PipAnim = PipAnim;
  if (typeof module !== 'undefined' && module.exports) module.exports = PipAnim;
})(typeof window !== 'undefined' ? window : typeof globalThis !== 'undefined' ? globalThis : this);

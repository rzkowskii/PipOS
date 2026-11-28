// Run with Node. --write records the baseline and --check detects source or result drift.
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const hash = text => createHash('sha256').update(text).digest('hex');
const production = 'crates/face/expressions.json';
const candidates = ['sad', 'laugh', 'love', 'embarrassed', 'scared', 'annoyed', 'smug',
  'confused', 'wink', 'bored', 'relieved'];
const referencePairs = [
  ['sad', 'concern'], ['scared', 'surprise'], ['love', 'attentive'],
  ['confused', 'curiosity'], ['laugh', 'joy'], ['embarrassed', 'loading'],
  ['annoyed', 'smug'], ['annoyed', 'bored'], ['annoyed', 'tired'], ['wink', 'confused'],
];
const WIDTH = 192;
const HEIGHT = 120;
const f32 = Math.fround;
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const fixed = value => Number(value.toFixed(6));

function expressionData() {
  const html = read('research/face-expression-poc/src/face-lab.src.html');
  const begin = '/* ---------- expression data ---------- */';
  const start = html.indexOf(begin);
  const end = html.indexOf('/* ---------- state ---------- */', start + begin.length);
  assert(start >= 0 && end > start, 'Face Lab expression-data delimiters changed');
  const data = html.slice(start + begin.length, end);
  // Evaluate only the local constant table and helpers. No document, timers or renderer.
  const output = vm.runInNewContext(data + '\nJSON.stringify({ channels: EYE_KEYS, rows: P })',
    Object.create(null), { timeout: 1000 });
  return { data, ...JSON.parse(output) };
}

function validEye(eye) {
  const [x, y, w, h, round] = eye;
  return eye.every(Number.isFinite) && w > 0 && h > 0 && round >= 2 &&
    f32(Math.abs(x) + w) < f32(1.5) && f32(Math.abs(y) + h) < f32(0.9);
}

function clampEye(raw) {
  assert(raw.every(Number.isFinite), 'Face Lab row contains a missing or non-finite eye channel');
  const eye = raw.map(f32);
  if (validEye(eye)) return eye;
  const [x, y, w, h, round] = eye;
  const width = clamp(w, 0.0001, 1.4999);
  const height = clamp(h, 0.0001, 0.8999);
  const moveX = 1.4999 - width;
  const moveY = 0.8999 - height;
  const fitted = [clamp(x, -moveX, moveX), clamp(y, -moveY, moveY), width, height,
    Math.max(2, round), ...eye.slice(5)].map(f32);
  assert(validEye(fitted), 'clamped row failed the production geometry test');
  return fitted;
}

function aperture(eye) {
  const [x, y, w, h, round, tilt, top, topSlant, topCurve, bottom, bottomSlant, bottomCurve] = eye;
  const cosine = Math.cos(tilt);
  const sine = Math.sin(tilt);
  const bulge = (2 ** (0.5 - 1 / round) - 1) * Math.min(w, h);
  const ellipseX = (w * w - h * h) / w;
  const ellipseY = (h * h - w * w) / h;
  return (px, py) => {
    // Geometry from eye.vert and sdEye in eye.frag. Double precision is
    // sufficient for ranking these sampled contours. No glow, grid or marks.
    const localX = cosine * (px - x) + sine * (py - y);
    const localY = -sine * (px - x) + cosine * (py - y);
    const ax = Math.abs(localX), ay = Math.abs(localY);
    if (ax > w + bulge || ay > h + bulge) return false;

    const xn = localX / w;
    const bow = 1 - xn * xn;
    const topHeight = h * (top + topSlant * xn + topCurve * bow);
    const bottomHeight = h * (bottom + bottomSlant * xn + bottomCurve * bow);
    if (localY > topHeight || localY < bottomHeight) return false;

    let tx = 0.70710678, ty = 0.70710678;
    for (let i = 0; i < 3; i++) {
      const ex = ellipseX * tx ** 3, ey = ellipseY * ty ** 3;
      const length = Math.hypot(w * tx - ex, h * ty - ey);
      const qx = ax - ex, qy = ay - ey;
      const scale = length / Math.max(0.000001, Math.hypot(qx, qy));
      tx = clamp((qx * scale + ex) / w, 0, 1);
      ty = clamp((qy * scale + ey) / h, 0, 1);
      const normal = Math.max(0.000001, Math.hypot(tx, ty));
      tx /= normal;
      ty /= normal;
    }
    const ux = ax / w, uy = ay / h;
    const delta = ux * ux + uy * uy;
    const diagonal = 2 * ux * uy / Math.max(delta, 0.000001);
    const base = (delta > 1 ? 1 : -1) * Math.hypot(ax - w * tx, ay - h * ty) - bulge * diagonal ** 2;
    const dTop = (localY - topHeight) / Math.hypot(1, h * (topSlant - 2 * topCurve * xn) / w);
    const dBottom = (bottomHeight - localY) / Math.hypot(1, h * (bottomSlant - 2 * bottomCurve * xn) / w);
    const lid = Math.max(dTop, dBottom);
    const corner = Math.max(0.04 - Math.abs(base - lid), 0) / 0.04;
    return Math.max(base, lid) + corner ** 2 * 0.01 <= 0;
  };
}

function mask(pose) {
  const left = aperture(pose.left);
  const right = aperture(pose.right);
  const pixels = new Uint8Array(WIDTH * HEIGHT);
  const area = { left: 0, right: 0 };
  for (let y = 0; y < HEIGHT; y++) {
    for (let x = 0; x < WIDTH; x++) {
      const px = (x + 0.5 - WIDTH / 2) / (HEIGHT / 2);
      const py = (y + 0.5 - HEIGHT / 2) / (HEIGHT / 2);
      const l = left(px, py), r = right(px, py);
      area.left += Number(l);
      area.right += Number(r);
      pixels[y * WIDTH + x] = Number(l || r);
    }
  }
  return { pixels, area };
}

function overlap(first, second) {
  let intersection = 0, union = 0;
  for (let i = 0; i < first.length; i++) {
    intersection += first[i] & second[i];
    union += first[i] | second[i];
  }
  return union ? intersection / union : 0;
}

function nearest(pose, options) {
  return options.filter(other => other !== pose).map(other => ({
    name: other.name, overlap: fixed(overlap(pose.pixels, other.pixels)),
  })).sort((left, right) => right.overlap - left.overlap || (left.name < right.name ? -1 : 1)).slice(0, 2);
}

function preflight() {
  const original = expressionData();
  const nativeSource = read(production);
  const { channels, poses } = JSON.parse(nativeSource);
  assert.deepEqual(original.channels.map(name => name.replace(/[A-Z]/g, char => '_' + char.toLowerCase())), channels);
  assert(original.rows.length > 0 && new Set(original.rows.map(row => row.id)).size === original.rows.length,
    'Face Lab expression IDs must be unique');

  const legacy = poses.filter(pose => !pose.audition);
  const audited = poses.filter(pose => pose.audition);
  assert.deepEqual(audited.map(pose => pose.name), candidates);
  const limits = {};
  for (const side of ['left', 'right']) {
    assert(poses.every(pose => pose[side].length === channels.length && pose[side].every(Number.isFinite) &&
      validEye(pose[side].map(f32))),
      `invalid production eye in ${side}`);
    limits[side] = channels.map((_, index) => {
      const values = legacy.map(pose => f32(pose[side][index]));
      return [Math.min(...values), Math.max(...values)];
    });
  }
  const clamped = [];
  const eyes = new Map(original.rows.map(row => {
    const pose = { name: row.id };
    for (const [side, prefix] of [['left', 'L_'], ['right', 'R_']]) {
      const raw = original.channels.map(channel => row.f[prefix + channel]);
      pose[side] = clampEye(raw);
      for (const [index, channel] of channels.entries()) {
        if (pose[side][index] !== f32(raw[index])) {
          clamped.push({ name: row.id, side, channel, from: raw[index], to: fixed(pose[side][index]) });
        }
      }
    }
    return [row.id, pose];
  }));
  const comparison = legacy.map(pose => ({ name: pose.name, ...mask(pose) }));
  const integrated = audited.map(pose => ({ name: pose.name, ...mask(pose) }));
  const evaluated = candidates.map(name => {
    const pose = eyes.get(name);
    assert(pose, `proposed expression missing from Face Lab: ${name}`);
    const outsideObserved = [];
    for (const side of ['left', 'right']) {
      for (const [index, channel] of channels.entries()) {
        const [low, high] = limits[side][index];
        if (pose[side][index] < low || pose[side][index] > high) {
          outsideObserved.push({ side, channel, value: fixed(pose[side][index]), range: [fixed(low), fixed(high)] });
        }
      }
    }
    return { ...pose, ...mask(pose), outsideObserved };
  });
  const originalMasks = new Map([...comparison, ...evaluated].map(pose => [pose.name, pose.pixels]));
  const tunedMasks = new Map([...comparison, ...integrated].map(pose => [pose.name, pose.pixels]));
  return {
    metric: 'Intersection over union of sampled solid-eye silhouettes, without colour, light or motion',
    sampleSize: [WIDTH, HEIGHT],
    sources: {
      preflight: hash(fs.readFileSync(__filename, 'utf8')),
      pocData: hash(original.data), productionPoses: hash(nativeSource),
      productionValidator: hash(read('crates/face/build.rs')),
      shaderVertex: hash(read('crates/face/src/eye.vert')),
      shaderFragment: hash(read('crates/face/src/eye.frag')),
    },
    channels,
    rows: original.rows.length,
    sourceFields: Object.keys(original.rows[0].f).length,
    clamped,
    candidates: evaluated.map(pose => ({
      name: pose.name, left: pose.left.map(fixed), right: pose.right.map(fixed),
      area: pose.area, outsideObserved: pose.outsideObserved,
      nearestProduction: nearest(pose, comparison), nearestCandidates: nearest(pose, evaluated),
    })),
    integrated: integrated.map(pose => ({
      name: pose.name, area: pose.area,
      nearestLegacy: nearest(pose, comparison), nearestIntegrated: nearest(pose, integrated),
    })),
    referencePairs: referencePairs.map(([left, right]) => ({
      left, right, original: fixed(overlap(originalMasks.get(left), originalMasks.get(right))),
      tuned: fixed(overlap(tunedMasks.get(left), tunedMasks.get(right))),
    })),
  };
}

const report = JSON.stringify(preflight(), null, 2) + '\n';
const baseline = path.join(__dirname, 'phase2-baseline.json');
switch (process.argv[2]) {
  case '--write': fs.writeFileSync(baseline, report); break;
  case '--check':
    if (fs.readFileSync(baseline, 'utf8') !== report) {
      process.exitCode = 1;
      console.error('Face Lab preflight drifted. Review its output before updating the baseline.');
    } else {
      console.log('Face Lab Phase 2 preflight matches the baseline.');
    }
    break;
  case undefined: process.stdout.write(report); break;
  default: throw new Error('usage: node phase2-preflight.js [--write|--check]');
}

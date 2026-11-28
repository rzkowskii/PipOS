// Export the Face Lab catalogue for Cargo without running the preview UI.
import { readFileSync, writeFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = new URL('../../research/face-expression-poc/src/face-lab.src.html', import.meta.url);
const target = new URL('concept-expressions.json', import.meta.url);
const text = readFileSync(source, 'utf8');
const start = text.indexOf('/* ---------- expression data ---------- */');
const end = text.indexOf('/* ---------- state ---------- */');
if (start < 0 || end <= start) throw new Error('Face Lab catalogue markers are missing');

const { P: poses, EYE_KEYS: eye, FX_KEYS: effects, BROW_D, MOUTH_D } = runInNewContext(
  `${text.slice(start, end)}\n({ P, EYE_KEYS, FX_KEYS, BROW_D, MOUTH_D });`, { console }, { timeout: 1000 },
);
const brow = Object.keys(BROW_D);
const mouth = Object.keys(MOUTH_D);
const details = ['sp_heart', 'sp_x', 'sp_spiral', 'sp_star', 'pup', 'shine', 'wet'];
const kick = ['pop', 'recoil', 'roll', 'burst', 'pupil'];
const tint = ['ac', 'acr', 'acg', 'acb'];
const rows = poses.map(({ id: name, f, kick: entrance }) => {
  const channels = (names, prefix = '') => names.map(channel => f[prefix + channel]);
  return {
    name,
    left: channels(eye, 'L_'),
    right: channels(eye, 'R_'),
    brows: ['bL_', 'bR_'].map(prefix => channels(brow, prefix)),
    mouth: channels(mouth, 'm_'),
    effects: channels(effects, 'fx_'),
    details: channels(details),
    kick: kick.map(channel => entrance?.[channel] ?? 0),
    tint: channels(tint),
    motion: channels(['gbx', 'gby', 'roll']),
  };
});
const schema = { eye, brow, mouth, effects, details, kick, tint };
const formatRow = row => `    {\n${Object.entries(row)
  .map(([key, value]) => `      ${JSON.stringify(key)}: ${JSON.stringify(value)}`)
  .join(',\n')}\n    }`;
const generated = `{
  "channels": ${JSON.stringify(schema)},
  "poses": [
${rows.map(formatRow).join(',\n')}
  ]
}\n`;

if (process.argv.includes('--check')) {
  if (readFileSync(target, 'utf8') !== generated) {
    throw new Error('Run node crates/face/export-concepts.mjs to refresh the Face Lab catalogue');
  }
} else {
  writeFileSync(target, generated);
}

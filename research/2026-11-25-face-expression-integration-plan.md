# Pip face: integrating the expression proof of concept into PipOS (plan v2, after review)

Date: 2026-11-25. Updated 2026-11-26. The renderer keeps the legacy poses and 11 tuned native auditions. Setup and voice can enable brows and mouth independently. Eyes-only remains the factory setting.

The native audition includes all 89 Face Lab expressions alongside `speaking` and `dreaming`, 26 effect controls, 13 actions and nine scenes. The expression tints use the authored colours. The Look picker includes all 11 named palettes, with native colour values for the six palettes the Shell Lab named without swatches. `--source-pose` switches the native shader from its tuned eye presets to the Face Lab’s authored input geometry. Glitch uses moving bands with steady brightness. The live face keeps its eyes-only factory setting and allows owners to enable every layer independently in Look. The Mac renderer, the GLES shader path, and the laptop’s HD 4000 rendered the expanded face. A live 1280×800 built-in panel capture passed with voice and cortex services active. The controlled power comparison remains open.

Proof of concept: `research/face-expression-poc/index.html` (89 expressions, brows, mouth, specials, emotes, kicks, scenes).
Evidence: code review plus hardware, lab-gate and product review that checked v1 of this plan against the repo. **[V]** = read in code or logs, **[I]** = inference. The hardware logs include frame pacing. Dedicated HD 4000 GPU render time remains unmeasured.

## 1. What changed from v1 (review corrections)

1. **Decor cannot draw in the current shader.** `eye.frag` returns early when `cover == 0 && halo == 0` (just after the eye loop). Anything beyond about 0.22 units from an eye (a mouth, hearts, notes, glyphs) is discarded, and the LED grid and final blend only reference eye coverage. v1's "decor loop is a few compares" was wrong. See 4.2.
2. **Decor bounds cannot be vertex varyings** (GLSL ES 3.00 guarantees 15 vec4 varyings; the eyes already use 6 slots). Bounds go in uniforms.
3. **Expression names were strings throughout `anim.rs`.** Phase 1 moved animation to `ExprId` and moved spring damping and anticipation flags into the table. Gaze and gesture logic still selects behavior by expression ID. `Spring::reflect` mirrors the 26 pose channels.
4. **v1 treated mixed activity as idle.** Attending, listening and speaking use the 60 Hz Active tier. In 156 low-cortex-activity minutes, the median was 38% of frames at 60 Hz and 0.89 W graphics power. That sample did not isolate microphone capture. A controlled idle baseline and a comparison of pacing versus package power are pending.
6. **The owner approved optional, customizable brows and mouth on 25 Nov (D1).** The device defaults to eyes only. The other decisions in section 7 are proposed defaults unless explicitly recorded as decided.
7. **There is no face-only deployment path.** `being-face` is cross-built inside Buildroot and shipped on a read-only image; lab `cable.sh push` only copies `mind/*.py`. Face changes ride the next stick image (and change the image source id, re-triggering the boot gate).
8. **Tint arithmetic was too loose.** The lab's cyan test applies to every lit pixel at every brightness, so a tint must satisfy `(b - r) >= 0.25 * (r + g + b)` and `g > r` and full-brightness `g >= 175`. That rules out true violet and most mint. Details in 5.
9. **Scope.** The minimum useful slice needs no shader change, no mouth, no colour change and no bus change (section 3).

## 2. Findings that still stand

- **The POC is not rendering like the laptop.** Same geometry and the same 26 eye numbers, but the real shader uses fill (8,219,255), core (77,250,255), halo (0,122,255), linear-light summing with a gamma-2 encode, an analytic halo (4.8 px skin plus a tail exactly zero at 88 px, never under the eye), a 3 px LED grid at 16 % depth on the eye only, and "never white". The POC uses lighter colours, a Gaussian canvas blur, a 30 % grid and white highlights. The POC eye is a polygon with a superellipse exponent, the shader is an ellipse plus a diagonal bulge, so numbers are not pixel-identical (POC Surprise h .50 / x -.72 vs real h .56 / x -.68).
- **Real transitions are in `anim.rs`, not `pace.rs`.** Closed-form per-channel spring (omega 14 to 20, zeta 0.64 to 0.82) plus tuned blink, saccade, breathing, gesture, drive and speech layers. POC spring feel is close (omega 13.8, zeta 0.62). Map onto the Rust spring; do not replace it.
- **Only a NAME crosses the bus**, only the brainstem emits `face.express`, and `anim.express` rejects names outside `eye::NAMES` (16 names including `speaking`, plus face-private `loading`). The 0.8B mind is grammar-limited to 7 tags. The brainstem relays `mind.emotion {name, hold}` as `face.express` copying only name and hold, so any new field must also be added in `machine.rs` and its tests.
- **The face is already power-sensitive.** Power and heat are a measured issue, which is why every extra needs a numeric budget.
- **First real-screen reference (a photo of the real panel, 25 Nov, not in the repo):**
  - **The panel's "black" is a blue wash, not black.** It is an LCD, so the backlight lifts the whole screen to a flat blue-grey, and the camera exposure exaggerates it. Dim decor (brows, a mouth at low glow, gloom lines, dim marks held under sum 120) will have far less contrast than in the POC's pure-black canvas. Test extras on the real panel, not only on a monitor. Marks kept at about 22 % brightness may be nearly invisible on the real panel, which is a separate finding.
  - **The pose shown is a half-lidded pleased dome** (flat concave underside, rounded top), the same family as the existing `content` squint, so the real eyes do carry a lot of character already.
  - **The halo is a thin, crisp, deeper-blue ring** hugging each eye, with no wide soft glow visible at this exposure. That matches the analytic skin (4.8 px) and confirms the POC's wide Gaussian bloom is too soft and too light.
  - **Colour:** the fill reads as saturated cyan with a hotter, lighter centre, shading to azure at the rim. No LED grid is visible at this distance. The screen occupies the lid with normal bezels, so the face fills the panel.
  - Consequence for Phase 4: the POC page should be re-aligned to this look first (deep ring, tight skin, no wide bloom), and any extra stroke should be designed with a lifted black level in mind.
- Hardware logs do exist (first real boot 23 Nov, cable audit 25 Nov), although `IMPROVEMENT-PLAN.md:22` still says nothing has run on the MacBook (stale).

## 3. Recommended MVP: eyes-only expansion (no owner decision needed)

Delivers visibly more range on the real laptop with the lowest risk.
1. **Data-driven expressions:** `config/expressions.json` embedded with `include_str!`. Row schema: `from: open|rest`, optional `mirror_from`, per-eye overrides, optional post-mirror `tilt`/`y` offset (needed for `listening`, whose tilt offset is applied unmirrored, and `thinking`, whose right eye is `rest.mirror()` plus overrides), `tier` (`model` | `brainstem` | `lab`), and per-row flags that replace string matches: `lively`, `damping`, `omega_scale`, `thought_side`, `kick`. Names become an `ExprId` (index into a `OnceLock` table) instead of `&'static str`.
2. **Golden equality first:** per-channel (26 floats) JSON == `preset()` within 1e-6 for the 16 names plus `loading`, in the same commit that keeps every `preset()` arm. Only later delete the match. A unit test parses and validates the embedded JSON, with a minimal built-in neutral fallback if loading ever fails. Add `serde_json.workspace = true` and commit `Cargo.lock` (the Buildroot build uses `--locked`).
3. **11 new eyes-only expressions** chosen for readability: sad, laugh, love (via a later heart morph, otherwise skip), embarrassed, scared, annoyed, smug, confused, wink, bored, relieved. Plus per-expression **gaze bias**, **roll**, **kick** (data replacing the hard-coded `acting == "surprise"`), per-expression blink and breath multipliers, **intensity**, and **nod/shake** gestures.
4. **Validators on every loaded row:** the existing eye.rs rules (|x|+w < 1.5, |y|+h < 0.9, round >= 2, mirror twice, array round trip), plus a post-spring overshoot check or clamp, since underdamped springs (zeta 0.64) overshoot.
5. **Redo Surprise** as the startle (pop, recoil, double blink already exists) using the data kick; keep the pinned tests `joy_arrives_with_a_little_hop` and `surprise_pops_and_blinks_twice` green.
6. **Verification:** `face --sheet` renders static presets only (poses, extras off). For motion use `--frames --seed --fps` with a script that sets `express`. Tolerance per channel; llvmpipe and HD 4000 differ in dither, so goldens need tolerances measured on both.

What the MVP does not touch: shader, lab detector, bus topics, model tags, colour.

## 4. After the owner decisions: shader extras (optional layer)

### 4.1 Gate
Extras (brows, mouth, pupil, wet, glyphs, tears, particles, tint) are separate optional layers chosen by the face style setting (4.5). **The default style is eyes-only**, on every build, including the lab image and your laptop, so the existing lab gates and first-boot face are unchanged unless someone opts in. They also force OFF: in the strip/app-panel layout (`panel_amount > 0`, where the face is scaled to 256x160 and a 0.02 unit stroke is about 2 px with the grid off), while the level bar is shown (it occupies x +/-0.77, y about -0.53 to -0.67, exactly where a mouth sits), while captions are open, and in muted, unwell, asleep, dim and low-battery poses. The sleep, dim, lid-wake, birth and muted paths in `Anim::frame` apply only to the eyes today, so each must also gate the extras.

### 4.2 Shader changes
- Restructure `main()` so a decor loop runs **before** the early-out and contributes to `lit`, `cover` and `halo` with its own g-squared bloom. Decide explicitly whether decor receives the LED grid (the grid is gated on `cover > 0`).
- Loop `for (i < uDn)` with `break`, so cost is zero with no extras. Decor bounds are CPU-computed uniforms (centre and half extent). Do not use per-item flat varyings.
- Special eyes: a CPU-side discrete `shapeId` plus a spring **morph amount** in `uE[i*4+3].yz` (do not spring an integer). Each shape needs its own `u`, `rmin` and conservative bound; heart and star SDFs are approximate, so bloom skin varies. Widening the uniform block changes `Pose::uniforms()`, `Renderer::draw_in`, `snapshot`, `stills()` and the strip path: a larger struct, not a fixed `[f32;32]`.
- Glyphs (? ! Z, check, warn, battery, dots) as SDF strokes, not the 8x16 bitmap quad. Notes: circle plus stem.
- Drop or defer: flames, snow, steam, spiral eye (80 to 120 ALU), most particles. Scan line is cheap (5 ALU). Glitch only as a rate-capped coordinate warp before the eye loop (at most 3 flashes per second, tested on `--frames` output).
- Compile risks: lab gates run on llvmpipe, which unrolls constant loops, and `WatchdogSec=5s` is first kicked after shader creation. Ship a decor-free shader variant for the software path, compile the eyes-only program first, and require `test_l1` power-on-to-eyes not to regress with a cold shader cache.
- Both headers must compile (`#version 330 core` and `300 es` with highp); avoid desktop-only features and implicit-derivative `texture()` in divergent flow.

### 4.3 Animation and activity
- Keep `Pose` and `[f32;26]` unchanged. Add a separate extras array (about 32 to 70 channels) with its own step function, grouped (brows and mouth about 0.94 times eye omega, fx and tint critically damped at omega 6.5 to 7.5, roll and gaze bias about 7.7). Per-group settle thresholds, since 2e-5 / 1.5e-4 are tuned for lid units. Extend `reflect()` to the extras.
- Omega is eased each frame in production, so 20 Hz vs 60 Hz equivalence is only approximate; add a test with varying omega and mid-step emotion expiry.
- Continuously animated extras must feed `activity()` explicitly and only while present.
- Mouth = static shape per expression plus an amplitude opening from `say.level` (about 25 Hz scalar, attack 44 ms, release 150 ms). No visemes: no phoneme timing on the bus, and adding some needs a new voice-to-face topic, an `allowed_from` entry and a privacy review.

### 4.4 Safe area and marks
Decor stays within |x| < 1.38 and |y| < 0.85 and keeps a dark band around the ears (x = +/-1.525), network dot (1.525, 0.915) and SSH mark (-1.54, 0.915). No shine near the camera glint at (-0.34w, +0.36h). Static brow or mouth lines need slow drift for burn-in. Note marks are listed "not built" in plan item 7.5 although partly wired, so the safe-area tests may be written against marks still being finished.

### 4.5 Face style settings (decided: customisable, optional, default eyes-only)

**Layers, each independently switchable:** `brows`, `mouth`, `effects` (glyph and particle emotes: tears, sweat, hearts, zzz, check, and so on), `eye_details` (pupil, wet, heart/star eye morphs), `tint` (mood colour, limited to the cyan-family rule in section 5). Eyes are always on.

**Named styles (presets of those layers):**

| Style | Layers on |
|---|---|
| `eyes` (default) | eyes only |
| `eyes+brows` | brows |
| `eyes+mouth` | mouth |
| `expressive` | brows, mouth |
| `full` | brows, mouth, eye_details, effects (tint stays separate) |
| `custom` | any combination of the five layers |

**Design rules this creates:**
1. **Every expression must read on eyes alone**, because eyes-only is the default. Layers add, they never carry the meaning. Expressions the POC built around a mouth (cheeky, flirty, disgusted, angry teeth) need an eyes-only rendering (lids, tilt, gaze bias, roll, kick), and the layer data is purely additive. Each row in `config/expressions.json` therefore has an `eyes` block that is complete, plus optional `brows`, `mouth`, `eye_details` and `effects` blocks.
2. **Layers fade, not pop.** Each layer has an alpha channel on the extras spring, so switching style fades brows and mouth in and out. A layer at alpha 0 costs nothing: the decor loop count `uDn` excludes it, its channels do not wake `activity()`, and no bounds or uniforms are uploaded.
3. **Eyes-only stays the lab's contract.** All existing lab gates run at style `eyes`. Extras tests run per layer using the `eyes_v2` detector from section 5.
4. **Constraints from 4.1 still apply per layer** (hide in the strip layout, under the level bar and captions, and in muted, unwell, asleep, dim and low-battery poses).
5. **Layer-specific behaviour:** the mouth opens with `say.level` (section 4.3); with the mouth off, the existing speech pulse on the eyes is unchanged. With brows off, lids keep doing the brow work they do today.

**Where the setting lives and how it changes (follows existing PipOS patterns):**
- **Persisted** in a small `face.json` beside `voice.json` (style name plus the custom layer flags), read by the face at start with `eyes` as the fallback when the file is missing or invalid. The face does not read `config/being.json` today (only `BEING_FACE_*` env flags), so this is a new, small persisted file.
- **Change at run time** the way brightness and volume work: the mind sends a request (for example `being.face_style` with `style` or `layers`), the brainstem validates and persists it, and emits a reserved `face.style` event that the face applies and answers with the state it really has. This needs an `allowed_from()` entry and tests in `brainstem/src/main.rs`, a row in `docs/PROTOCOL.md`, and mind intents ("show your mouth", "eyes only"). Optional and later; not needed for the first release of the layers.
- **Setup screen:** a face style picker in the appliance setup (F2, `being-local-screen`). I did not read that console app, so how settings are added to it is unverified.
- **Developer and lab:** `face --style full`, `--layers brows,mouth`, and the same flags for `--sheet`, `--snapshot` and `--frames`, plus an env override (`BEING_FACE_STYLE`) for the cable stick. The browser preview already has display toggles; it should use the same style presets and names so what you review matches the device.

## 5. Colour, lab and test rules

- **Tint rule (arithmetic):** a tint passes everywhere only if `(b - r) >= 0.25 * (r + g + b)`, `g > r`, and `g >= 175` at full brightness, evaluated also on the dimmed-eye colour (rim mixes toward halo) and through the roll-off. Keep halo red near 0. Allowed: teal, cyan, azure, cornflower (hue about 170 to 235). Not allowed: true violet, pink, red, gold, most mint (mint (120,255,200) fails for brightness k of 0.21 to 0.375). The unit test sweeps k in [0.2, 1] and glow in [0.35, 1] through the shader's own arithmetic. Default MVP stays cyan-only; whether warm hues are ever allowed is your call.
- **Lab detector:** leave `eyes()` unchanged for existing gates and run them with extras OFF. For extras-ON add `eyes_v2`: the two components containing the brightest core pixels (G >= 200 over an area), returning boxes and the in-component maximum G (not a box-centre sample), classifying decor by position. A plain "two largest blobs" is wrong (a 150x60 mouth outranks a muted 22 to 80 px slit, and brows hugging the eye merge via its bloom). Cyan fraction is global (`> 0.95`), so any non-cyan lit pixel fails it regardless of component splitting. Consumers to review: `test_face_usb` (G sampled at box centre, ratios 0.42 to 0.78 tired, 0.55 to 0.86 muted), `test_l6` (gaze from box edges), `test_apps`, `test_local_screen`, `test_laptop_smoke`.
- **New tests:** tint passes inequalities; decor never overlaps marks or the level-bar rect; photosensitivity cap on `--frames`; `activity()` Calm with no extras; settle across new channels; post-spring clamp; parse/validate embedded JSON; golden stills with tolerance.
- **Unverified:** the "19 of 19" local-screen figure could not be re-found by the second reader; do not cite it until sourced.

## 6. Hardware, power and measurement

- **Phase 0 (before judging extras):** record a clean idle baseline on the cable stick with voice and dictation stopped (10+ minutes idle, then attending with no speech, then speaking), reading the existing per-minute log: PP1 W, rate split, before-swap ms, rc6 and rc6p deltas, i915 IRQ/s, flip-worker D-state fraction. Log the GL profile in use (3.3 core vs GLES 3.0).
- **Possible idle fix:** if clean baselines show it, drop `engaged() && !muted` (`anim.rs:724`) to Calm while attending or listening is spring-settled with no level or gaze motion. Do not drop blinks below 60 Hz (about 4 frames at 30 Hz). This belongs to plan item 2.1.
- **GPU timer:** `GL_TIME_ELAPSED` is core only on desktop GL 3.3; GLES 3.0 needs `EXT_disjoint_timer_query`.
- **Numeric budget for extras (proposed):** extras ON at Calm adds at most 0.1 W PP1 over extras OFF across 10 minutes on AC, rc6 does not fall, cold-cache link time and `test_l1` power-on-to-eyes do not regress. Report AC and battery.
- **Deployment:** face-only candidate testing needs either a builder-volume target building only `-p face` plus an `ExecStart` drop-in copying a binary to `/run`, or folding the face into the next cable/round-2 image with extras OFF by default. The frozen first-boot stick is never rebuilt (`STATUS.md:13`).

## 7. Decisions for you (safe default first)

| # | Decision | Default |
|---|---|---|
| D1 | Adopt brows and a mouth on the real Pip? | **DECIDED by the owner, 25 Nov: yes, as optional, customisable layers. Default is eyes-only.** Record as a new row in `docs/DECISIONS-OWNER.md` and update the four "no mouth" docs to say "eyes by default, optional brows and mouth". See 4.5. |
| D2 | Mouth role | Static shape plus `say.level` opening; no visemes. (Now that the mouth is approved as optional.) |
| D3 | Colour: cyan-only, or the narrow blue-ward tint set in section 5, or warm hues | Cyan-only. |
| D4 | Grow the model's tag set beyond 7 | Not now (only 6 of 24 replies complete today). Real coupled files: `mind/speech.py` (EMOTIONS, grammar), `config/identity.md`, `tests/test_speech.py`, `tests/test_mind.py`, `mind/brief.py:98-105`, and plan item 8.3. Persona-LoRA 7.6 is future, not current. |
| D5 | "Never white" highlights | Keep. Use core-coloured highlights away from the camera glint. |
| D6 | Honest-body rule for emotes | Only state-true emotes (battery, sleep, listening, thinking). Tears, sweat and hearts are not true statements about a laptop; keep them lab-only unless you approve (`docs/DESIGN.md:56`). |
| D7 | Own-character check | Review brows, mouth and emotes against "its own shapes" (`DESIGN-AND-PLAN.md:156`, EVE as genre reference only). |
| D8 | First-boot face veto | You approve any change to the first-boot face before the hardware run. |
| D9 | Photosensitive effects (glitch) | Out of scope unless approved, and tested if kept. |

Sequencing (not a decision): the 60 Hz idle tweak, face-only deployment path and image timing sit in section 6.

## 8. Phases

0. **Measure (no visible change):** clean idle baseline, GL profile log, photo of the live face, decide the per-minute log fields. Eyes-only work does not wait on this; only extras do.
1. **Data-driven expressions (complete in source):** The pose catalogue builds into a validated `ExprId` table. The old poses are the golden in tests. Every channel and 48,000 sampled animation steps matched exactly on the host. [Catalogue](../crates/face/expressions.json)
2. **MVP expansion:** The renderer auditions 11 tuned poses with gaze bias, roll, kicks, intensity, nod and shake. Surprise has a revised pop, recoil and double blink. The seven model tags stay in place. The preflight finds less static contour overlap in all ten tracked comparisons. Owner readability still needs an audition. [Preflight](face-expression-poc/phase2-preflight.js)
3. **Owner decisions D1 to D9.**
4. **Parity lab:** Face Lab opens with the production WebGL2 eye shader at 1280×800, uses the shared catalogue for names available in the appliance, and keeps the concept layers available in a separate view. Native panel calibration and readability testing are open. [Face Lab](face-expression-poc/index.html)
5. **Extras:** Independent brows and mouth use the existing shader and persisted Look settings. Mouth opening follows speech level. Layers fade on preference changes and stay hidden in the app strip, during caption and volume overlays, and in protected poses. The numerical GPU budget, a detector for decorated faces, and any additional layers still need qualification.
6. **Wiring and docs:** `machine.rs` carries `intensity` if wanted; states stay face-local (`state.changed`, `body.state`; no `task_done` event exists, so a success glyph would need a new brainstem-emitted event with `allowed_from`, PROTOCOL.md and tests, or is dropped); update README (17-pose count, "no mouth"), `docs/DESIGN.md`, `DESIGN-AND-PLAN.md`, `docs/PROTOCOL.md` (already stale: 15 listed vs 16 names plus `loading`), `docs/expressions.png`.
7. **Hardware validation:** the face lands in the next image; run the quiet-power A/B on the cable stick before round-2 voice tests, record in the same PIPLOGS format, and keep face-source-id logging.

## 9. Launch set (corrected)
Existing: the 16 `NAMES` (neutral, attentive, listening, thinking, speaking, joy, curiosity, surprise, skepticism, concern, sleepy, asleep, dreaming, tired, muted, dazed) plus face-private `loading`. Low battery is an overlay of the idle pose from `body.state`, not a name. New (MVP): sad, laugh, love, embarrassed, scared, annoyed, smug, confused, wink, bored, relieved. Second wave: excited, proud, idea, determined, disappointed, guilty, grateful, hopeful, amused.

## 10. Open items
- One more phone photo or two: the idle neutral face, and with the room lit and dark (the first photo is a half-lidded pose in dim light). Also save the photos into `reference/` so the plan can cite them.
- Which GL profile the laptop uses (readable from `main.rs:218`/logs).
- Intended scheduling: realtime plus sibling affinity (design text) vs `CPUWeight=1000` only (shipped unit).
- Check for collisions with other in-progress face work before merging.
- Strip layout: this plan assumes eyes-only in the strip, whatever the style.
- Style setting surfaces: confirm the setup screen (`being-local-screen`) can host a picker, and whether voice control of the style is wanted at all.
- Keep the POC as the permanent authoring tool (exporting JSON), or a viewer once the table is in Rust.

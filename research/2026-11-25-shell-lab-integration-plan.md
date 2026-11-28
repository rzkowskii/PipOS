# Shell Lab: aligning it with the real face and bringing it into PipOS

Date: 2026-11-25. Updated 2026-11-26. Browser parity, product Look preferences, shared help, the caption route indicator and Calculator are in source. The installer has the first product visual slice. The findings below describe the 25 Nov starting point.
Proof of concept: `research/shell-installer-poc/index.html` (Shell Lab, version 3: installer, quick menu, options, widgets, Monitor, text lookup, Calculator and voice router).
Companion plan: `research/2026-11-25-face-expression-integration-plan.md` (Face Lab). The hardware baseline in Phase 0 is shared. Face Lab's Phase 1 moved the existing product poses into a data table. This plan's Phase 1 aligns the browser renderer.

Evidence: code review of the face renderer, face alignment, bus and brainstem, mind apps and voice, installer and OS, and hardware and performance, with the key claims re-checked against the source. **[B]** = built (read in source), **[D]** = documented intent, **[I]** = inference. Line numbers are against the working tree on 25 Nov 2026, which has **uncommitted edits** in `crates/face` (`main.rs`, `link.rs`, `panel.rs`, new `keys.rs`), `crates/brainstem`, `crates/bodycare` and about 39 `mind/` files from parallel work. Rebase every step below on that work once it lands.

## 1. Headline findings

1. **How the eyes really render. [B]** `being-face` (Rust) owns the display through SDL2 `kmsdrm` with no window system (`crates/face/src/main.rs:1-4`, `being-face.service`), asking for GL 3.3 Core and falling back to GLES 3.0. The laptop ran GLES 3.0 on Mesa crocus (`docs/manual/ARCHITECTURE.md:256-262`). One full-screen triangle and one fragment shader (`eye.frag`, 205 lines) draw two signed-distance eyes (an ellipse made squarer by a `round` exponent, cut by parabolic lids) with linear-light bloom, a 16% LED grid and gamma-2 output. Its uniforms are `uRes, uE[8], uFill, uCore, uHalo, uGrid, uPitch, uGlowR, uOrigin, uMarks[2]`. Motion is `anim.rs`: an underdamped spring over 26 numbers towards 16 named presets plus `loading` (`eye.rs:142-216`), with blinks, saccades, breathing, a speech envelope and gestures layered on top. A textured-quad program draws CPU-rasterised RGBA pictures (workshop panel, F1 captions, level text) using a 148-glyph JetBrains Mono table.
2. **What is measured on the real MacBook. [B]** The first boot (23 Nov) drew the face on the HD 4000. The cable session (25 Nov, `out/hardware-logs/cable-20261125/audit-body/face-log.txt`) shows 60 Hz for 90-100% of most minutes, 15-16.6 ms frame work that is almost all vsync wait (0.1 ms before the swap), 1.1-1.4 W graphics power, 1.9% of one logical CPU and 44-52 MB RAM. The GPU never left its 350 MHz floor, but RC6 residency was only about 5% and the package never reached deep C-states (about 8-9 W at 71-82 °C near idle). The always-60 Hz face is a suspect for that, not yet proven. **CPU is the scarce resource. GPU and RAM have room** (14.7 GB free beside the model).
3. **Shell Lab's face is not Pip's face. [B]** It draws portrait rounded rectangles on a 2D canvas (`index.html` `eye()`, `rr()`), with a white-topped gradient, `shadowBlur` glow, a sine blink and 11 ad-hoc moods. Geometry, colour, lids, blink timing and glow all differ. Its "cyan" `[45,239,255]` is not the real triple (fill `[0.03,0.86,1.0]`, core `[0.30,0.98,1.0]`, halo `[0.0,0.48,1.0]`, `render.rs:75-85`). The PipOS website already shows `eye.frag` compiling unchanged under WebGL2 (`PipOS-Website/src/index.html:2143-2164`), but as a hand-pasted copy without `uOrigin`/`uMarks`. Its animation port is stale (`anim.rs` no longer matches `docs/media/SOURCES.sha256`; the shader files still do).
4. **The real screen model is one panel plus a 160 px eye strip. [B]** When an app opens, the eyes glide into a 256x160 viewport at the top centre over 0.45 s and one 1280x640 panel fills the rest (`panel.rs:71-74`, `layout()` at 568-584). This is owner answer 10 (`docs/design/voice-apps.md:987`). Panels are `being-console --panel` renderers writing BPN1 frame files to `/run/being-panel/NAME.frame`, driven by `hands`. Only terminal, monitor and clock exist. Care, Setup, the Try menu and the installer are a separate full-screen console (`being-local-screen.service` has `Conflicts=being-face.service`), so the face stops while they are up.
5. **The POC's desktop runs against a standing decision. [D]** `docs/DESIGN.md` §12 says: "No desktop, window system, package manager or login shell on the screen." Shell Lab's several overlapping windows, quick menu and Firefox are a desktop. That is the biggest decision in this plan (S1 below). Most of the POC's value (look options, captions with a cost chip, voice-for-everything, corner widgets, a quick menu, more surfaces, a better installer) fits inside the one-surface model without a window system.
6. **Eye colour, shape and glow are compile-time constants. [B]** `main.rs` builds `Look::default()` and only marks and grid change afterwards. No bus topic or flag reaches it, although the draw call already takes `&Look` per frame, so a runtime look is a small change. The lab finds the eyes by `b > r+30 and g > r` (`lab/vm.py:604-616`), and `test_local_screen` wants cyan above 0.95. **Of the POC's nine glows only cyan and blue pass.** Green, amber, red, rose, yellow, violet and white fail.
7. **The mind already routes code first. [B]** `Mind._run_turn` (`mind/turn.py:2117-2240`) tries pending answers, guest commands, workshop apps, `commands.parse` and everyday skills before the model, and logs a route name per turn that never carries the words (`turn.py:2104-2115`). The docs say code answers cost 0 model tokens (`docs/PIP-FUNCTIONS.md:92`). Missing: any on-screen cost or route display, any look or layout setting (profile holds name, owner, wake word, voice, rate and personality only, `profile.py:61-67`), one registry shared by voice and menus, and a UI "subject" register for follow-ups like "actually, put it on the left" (only a memory referent exists, `turn.py:730-735`).
8. **There is effectively no pointer. [B]** A left click or touch sends `key.touch` (and sometimes `key.attend`) with no coordinates. There is no motion or scroll, and the cursor is hidden in the appliance. A clickable quick menu or widget close box needs pointer work. Keyboard and voice must come first.
9. **The real installer is honest and text-based. [B]** It is a keyboard UI (`mind/local_menu.py`, `installer.py`, `install_apply.py`) inside `being-console`, with small CPU-drawn eyes, not the shader face. Flow: choose a disk, Try settings, Ready to install, a case-sensitive `ERASE /dev/sdX` plus `ERASE MACOS|BEING|DRIVE` (`installer.py:334,438`), four real progress stages, then pull the stick. The layout is ESP 512 MiB, two 2 GiB system slots, models sized at 2x the live model plus 96 MiB, and the mind takes the rest. There is no resume after a power cut: the disk will not start and the owner reinstalls from the untouched stick. Against that, these POC parts are **theatre**: 6 GB slots, 8 GB models, a bare `ERASE`, power-cut resume, `rauc`/`efibootmgr` log lines, the boot and rollback self-test, network and camera choices carried into the install, macOS "118 GB used", and eye colour.
10. **Firefox does not fit. [B/I]** The image has no window system, toolkit, fontconfig or system fonts. The lab slot is 1 GiB, and CPU headroom goes to the model. Weather exists only as an online skill under Internet mode (`mind/weather.py`, `network_policy.py`).

## 2. Decisions needed from you

| # | Decision | Options | Recommendation |
|---|---|---|---|
| S1 | Desktop scope against DESIGN §12 | (a) Keep "eyes plus one surface", add the POC's look, chrome and surfaces inside it. (b) Allow two surfaces side by side. (c) Free windows. | **(a) now.** Revisit (b) after the laptop measures panel-renderer cost. (c) breaks §12 and the CPU budget. |
| S2 | Where the eyes go when a surface opens | Keep the top-centre 160 px strip (answer 10), or add a corner dock and face size options | **Keep the strip.** Make Shell Lab match it (0.45 s, top centre). Offer "face size" only for the empty desktop. |
| S3 | Eye colours | Lab-safe only (cyan default, blue, plus any teal or violet-blue that passes), or all nine with a palette-aware lab | **Lab-safe now**, warm colours after the lab detector learns the active palette (Phase 0). Each colour is a fill/core/halo triple, never white at the core. |
| S4 | Captions default | On (POC) or off (PipOS today, for privacy, `overlay.rs:1-7`) | **Keep off.** One voice or Options toggle turns them on. The cost chip appears only with captions. |
| S5 | Firefox and weather | Ship a browser, a text "look it up" panel, or nothing | **No browser.** A text lookup panel and a weather widget only in Internet mode, otherwise not shown. |
| S6 | Pointer | Keyboard and voice only, or trackpad coordinates with an auto-hiding cursor | **Keyboard and voice first.** Add coordinates after the laptop shows that SDL `kmsdrm` gives usable trackpad motion. |
| S7 | Terminal run rule | Docs and POC say runs only on "run it" (`PIP-FUNCTIONS.md:181`). Code runs one-liners and file-less recipes after a 4 s cancellable countdown (`hands.py:96, 2293-2300`) | Pick one and fix the other. I suggest "run it" for anything Pip wrote and the countdown only for built-in recipes. |
| S8 | Install after a power cut | Honest "start again from the stick" screen, or a journaled resumable install | **Honest screen now.** Resume is a separate project (the writer invalidates first and commits the GPT last, so resume is not trivial). |
| S9 | Models partition | `DECISIONS.md:20` says 48 GB in the title and 24 GiB in the body. Code sizes it at 2 x used + 96 MiB (`installer.py:66-69`) | **Code is the truth.** Fix the decisions row. The POC shows the real numbers from `installation_plan()`. |
| S10 | Face CPU share | `being-face.service` has `CPUWeight=1000`. `IMPROVEMENT-PLAN.md:153` planned 500, Nice -5, one llvmpipe thread | Decide before adding chrome. Any CPU-side UI work comes straight out of decode at 1000. |
| S11 | Installer UI home | Restyle `being-console` screens, or a new installer mode drawn by `being-face` | **Restyle `being-console` first** (it already owns KMS with GLES in Try). Move to a face-drawn installer later if the shader eyes are wanted there. |

**Answered by the owner on 25 Nov 2026: the recommendation for all eleven.**
- S1 eyes plus one surface.
- S2 top-centre strip.
- S3 lab-safe colours now, more after the lab detector change.
- S4 captions off by default.
- S5 no browser, with lookup and weather only in Internet mode.
- S6 keyboard and voice first.
- S7 "run it" for anything Pip wrote, countdown for built-in recipes only.
- S8 honest power-loss screen.
- S9 code sizes win, so fix `DECISIONS.md`.
- S10 measure before changing `CPUWeight`.
- S11 restyle `being-console` first.

## 3. Target architecture

### 3.1 Who owns what
- **Mind:** preferences and intent (look, menu position, widgets, captions), a single action registry, follow-up context, and the facts behind every surface. It persists preferences in `/mind`.
- **Face:** pixels and time. Geometry, animation, chrome drawing (quick menu, widgets, caption chip), hit-testing once a pointer exists, and echoing what it actually applied.
- **Hands and panel renderers:** surface content, as BPN1 frame files, never over the bus (the 64 KB line cap stays).
- **Brainstem:** routing, `allowed_from` gating and lid gating for every new topic, with tests.

### 3.2 New bus topics (all need `allowed_from` entries and `PROTOCOL.md` updates)
| Topic | From → to | Body | Notes |
|---|---|---|---|
| `screen.prefs` | mind → face, retained | `{glow, shape, strength, size, menu, menu_labels, widgets{clock,weather,system}, wpos, captions, cost, heard, gaze}` | Applied idempotently. Unknown keys ignored. |
| `screen.prefs_state` | face → mind | what the face applied | Lets voice replies say what is true ("the menu is on the left") and drives follow-ups. |
| `ui.action` | face → mind | `{id, via:'key'|'click'}` | Same handler as the spoken phrase. Logged as route `gui.<id>`. |
| `mind.route` | mind → face | `{route, by:'code'|'recipe'|'model', tokens, ms}` | Never the words. Nothing in guest mode. Lid-gated like other mind topics. |
| `screen.widget` | mind → face | `{id:'weather', text, until}` | Only for data the face cannot read itself (weather in Internet mode). |
| `app.open` (existing) | mind → hands | add `memory, lists, journal, mind, calc` | New panel renderers. |

The face also needs preferences **before** the mind is up, so the first frame is not default cyan. Recommended: the mind writes `/mind/look.json` and mirrors it to `/run/being/look.json`. A `status.rs`-style background thread in the face reads it at start and once a second (`status.rs:1-3,82-88` already does this for guest and network). Check on the laptop that the file is readable before the face's first frame (the unit runs with `DefaultDependencies=no`).

### 3.3 Face changes (small, mostly CPU-free)
- **Runtime `Look`:** a palette table (name → fill, core, halo), generated for Rust and the browser, validated by the lab colour rule in a unit test. Zero shader cost.
- **Shape bias:** a global offset on `round` after the spring and before `uniforms()` (soft = 0, sharp ≈ +1.2 to +1.7, round = clamp to 2.0). Keep the `round >= 2` test and leave the presets untouched.
- **Glow strength:** `glow_r x s` and per-eye glow `x sqrt(s)`, clamped so the core threshold and the 0.92 roll-off still behave (`eye.frag:137-153`).
- **Face size:** only for the empty desktop, by scaling the full-face eye rect. The docked strip stays as answer 10 fixes it.
- **Chrome** (quick menu rail, corner widgets, compact caption with the cost chip): textures rasterised with the existing font **only when their content changes** (the clock once a minute) and drawn with the existing quad program. Chrome must never put `activity()` into the Active tier (`anim.rs:710-730`). The font has no accented Latin letters, so a name like "Zoë" cannot be shown yet.
- **Voice meter bars and thinking dots:** do not port. The real face already shows speech through the lids (the level envelope raises h by about 8-10% and glow by 10.5%), and "thinking" through gaze search hops.

### 3.4 One action registry in the mind
A new `mind/actions.py`: `Action(id, label, surface, phrases, safety in {safe, address_only, destructive, strict_confirm}, handler(ctx, slots) -> Reply)`. From it, generate: the voice parse (consulted where workshop and everyday skills run today), the "what can I say" help panel, the quick-menu items, the ASR grammar phrase list, and the route and cost log. A small RAM-only `subject` register (menu, widget, surface) handles "actually, put it on the left" and "bring it back", mirroring Shell Lab's `S.subject`. Existing narrow mechanisms (pending yes/no, `AppContext` "close it", the lists undo) move under it over time. The POC review rounds already found the traps to design out: "forget it" must not confirm, "yes please" must confirm, and an unanchored keyword route must not beat an anchored one ("add red light bulbs to my shopping list" must not turn the eyes red).

### 3.5 One source of truth for the face in the browser
1. A build step inlines `crates/face/src/eye.vert` and `eye.frag` **verbatim** with the header `render.rs` prepends, for the website, Face Lab and Shell Lab, and records SHA-256 values in a manifest like `docs/media/SOURCES.sha256`. A lab test fails on drift.
2. Later, split `eye.rs`, `anim.rs` and the activity part of `pace.rs` into a dependency-free `face-core` crate built natively and for `wasm32`, so the browser runs the exact animation. `pace.rs` uses `std::time::Instant`, which panics on `wasm32-unknown-unknown`, so time must come from the caller.
3. Shell Lab's moods map onto real names: happy/glad → joy, curious → curiosity, working → thinking, serious → skepticism, sad → concern, surprised → surprise, listening → listening, sleepy → sleepy.

### 3.6 The neural awakening, honestly
- **Installer:** a GLES program in `being-console` with one static VBO of about 440 points and 1,300 lines, projection in the vertex shader, additive blend, capped at 30 Hz. Driven only by the real stage and byte callbacks (install is bound by USB 2 at about 23 MB/s, so the CPU is mostly waiting). Labelled as an illustration, as the POC already does. The five regions map to real stages: Body (preparing the disk), Senses and Language (writing the system and the model pack), Memory (creating the mind partition), Self (the GPT commit and the first wake).
- **Mind surface:** the same program at 15 Hz or a still frame, frozen while the cortex is generating, beside a RAM-only list of recent `mind.route` entries.
- **Night shift:** no lid-closed overlay. The real shift runs with the panel dark, and `mind.dreaming` marks only dream work (`PROTOCOL.md:82`). Instead, show "what I did last night" on lid open, from the action log.

## 4. Feature map: Shell Lab to PipOS

| Shell Lab feature | In PipOS today | Plan | Effort | Phase |
|---|---|---|---|---|
| Pip's eyes | Canvas2D rectangles vs real SDF shader | Render the real shader via WebGL2 in the POC | S | 1 |
| Moods | 11 ad-hoc vs 16 presets | Map table (3.5) | S | 1 |
| Dock when a surface opens | Top-left, 180 px, 0.8 s vs top-centre strip, 0.45 s | Match the strip in the POC | S | 1 |
| Eye colour (9) | Fixed cyan | Palette table, `screen.prefs`, lab-safe set (S3) | M | 2 |
| Eye shape, glow strength, face size | None | `round` bias, glow scale, empty-desktop size | S | 2 |
| Captions, heard line, cost chip | F1 overlay, off by default, no cost | Compact caption mode, `mind.route` chip | M | 3 |
| Voice for every action, follow-ups | Code-first routing, no registry, no UI subject | `mind/actions.py`, subject register | L | 3 |
| What can I say | Docs only | Help panel generated from the registry | S | 3 |
| Quick menu rail (left/right/top/off) | None. Mission Control opens the console | Face-drawn rail, keyboard focus, `ui.action` | M | 4 |
| Widgets (clock, system, weather) | Clock is a full panel; body data on the bus | Face-drawn corner textures; weather only online | M | 4 |
| Options app | Setup has profile fields only | A Setup "Look" page and a panel, same setters as voice | M | 4 |
| Terminal | Built (hands, recipes, cage) | Reuse; resolve S7 | S | — |
| Monitor: body | Built (`app_monitor.py`) | Reuse | S | — |
| Monitor: processes, safe restart | Doctor restarts automatically | Read-only list of `being-*` units; allow-listed restart through the doctor behind a strict yes | M | 5 |
| Memory surface | Voice and Care console page | Read-only panel; edits go through the same forget and correct paths | M | 5 |
| Lists surface | Voice only (`lists.py`) | Read-only panel over `lists.db` | M | 5 |
| Journal surface | Care dream-journal page, diary voice | Panel: day record plus action log | M | 5 |
| Mind surface | Route log on stderr only | Panel with illustration and recent routes | M | 5 |
| Calculator | Voice (`calc.py`) and the draft Calculator panel | Focused keyboard entry and display of the last sum while open | S | 5 |
| Care: network, camera, guest, backup, update | Console Care and Setup | Expose each control as a registry action | M | 5 |
| Firefox | Nothing; out by design | Do not ship (S5) | — | — |
| Night shift overlay | Dark panel during the shift | "Last night" summary on lid open | S | 5 |
| Installer cards, hardware rows, disk bar | Text menu, `hardware_rows()`, `installation_plan()` | Restyle; show only real rows and real sizes | M | 6 |
| Typed confirmation | `ERASE /dev/sdX` plus a second word | Keep exactly | S | 6 |
| Install progress and awakening | Four real stages with a byte meter | GLES scene driven by real progress | L | 6 |
| Power-cut resume | None | Honest screen (S8) | S | 6 |
| Personalisation (name, colour, announcements, restore) | Setup has most; restore is its own path | Seed `look.json` and existing profile fields | M | 6 |
| Narration during install | Mind runs in Try, no topic | Fixed code lines per screen, respecting mute and lid | M | 6 |

## 5. Phases

**Phase 0: measure and de-risk** (shared with the Face Lab plan). Photograph the live face. Add a GPU timer query if the selected GL context supports one. Saved cable logs show both Calm and Active pacing. The quiet baseline and controlled comparison still need to establish whether pacing affects package C-states and power. The [palette-aware detector](../lab/eye_pixels.py) has host fixtures and recovered-screen coverage. The original cyan-only boot check remains in place. S1–S11 are decided. *Exit:* baseline frame, power and C-state numbers recorded, and the lab passes.

**Phase 1: one face, everywhere** (browser only). The shared shader build step and hash manifest; Shell Lab and Face Lab draw through the real `eye.frag` in WebGL2; mood mapping; strip dock matching `panel.rs`; the palette table generated once. Update Shell Lab's installer numbers and confirmation to the real ones. *Exit:* a browser frame matches `being-face --sheet` for the same preset within a pixel tolerance.

*Phase 1 status (26 Nov 2026): both browser labs open with the production shader. The Face Lab concept controls also have a separate Canvas2D view.*
- **Preview source:** `research/shell-installer-poc/build.py` makes `index.html` from `src/shell.src.html`. It inlines the production eye shaders and uses the combined native and concept pose catalogues. It reads the palette swatches from `colour.rs`, the graphics defaults and mark level from `render.rs`, and the strip geometry and transition from `panel.rs`.
- **Drift check:** `python3 build.py --check` checks both the generated page and the manifest against the preview inputs and native source.
- **Renderer:** `src/face-gl.js` ports `render.rs` `draw_in`, `grid` and `linear`, plus `panel.rs` `layout` and `gaze_at`.
- **Animation:** `src/face-anim.js` ports `anim.rs`. It matches Rust to within 5e-6 over about 166,000 frames on the same seed, and 17 of `anim.rs`'s tests are ported.
- **Pixel parity:** compared with `being-face --sheet` at 640x400 for the six sheet presets. Surprise is identical. The others differ by a mean of 0.07/255 or less, with at most 0.15% of channel values more than 8 apart, on lid edges and smooth gradients. That fits WebGL-on-Metal against native GL precision.
- **Decisions applied in the POC:**
  - **Surfaces:** one surface under the strip; no Firefox; a lookup surface and weather widget only with the network on.
  - **Colours:** the lab-safe palette is cyan, blue, teal, mint and violet. Warm colours are refused, with the reason given.
  - **Terminal:** built-in script files wait for “run it” or an explicit terminal `run`. The POC demonstrates script files. Production also has a countdown for built-in one-line recipes. Verify the POC consent flow with `node --test test-terminal.js` after generating the page.
  - **Installer:** the real installer numbers and the two-step `ERASE` challenge.
  - **Power cut:** "start again from the stick".
- **Glow strength:** a rule worth keeping for Phase 2. The per-eye glow multiply is floored at min(preset, 1) and capped at max(preset, 1.2), so the shader's core threshold and roll-off behave.

**Phase 2: preferences.** `/mind/look.json`, the `/run` mirror, `screen.prefs` and `screen.prefs_state`, runtime `Look`, shape, strength and size, a Setup "Look" page and voice phrases sharing one setter. *Exit:* "Hey Pip, make your eyes blue" works on the laptop, survives a face restart without a cyan flash, and the lab stays green.

The product implements cyan and blue, shape, glow, empty-screen size, and independent brows and mouth through Setup and spoken commands. The face reads the mounted state before drawing. The dock keeps its original size. Host tests cover the renderer settings, shared validation, voice routing and retained state on reconnect. The two optional desktop widgets share the Look store and both default off. First-frame and speech checks on the laptop are pending.

**Phase 3: the action registry and the route chip.** `mind/actions.py`, the subject register, `mind.route`, the compact caption with the chip, and the generated help panel. *Exit:* every Options control has a spoken phrase, and a test walks the registry to prove it. The phrase-routing test (`tests/test_owner_phrases.py`) covers the new phrases and near-misses.

The action registry supplies spoken help, the Voice commands page in Setup, and the examples next to each Look setting. Tests walk the examples and replay the appearance controls through the running mind. The opt-in captions display a text-free route indicator and clear their content when a guest arrives. The Quick Menu uses the same registry for app selection. Context for other kinds of follow-up remains open.

**Phase 4: chrome.** Quick menu rail and corner widgets drawn by the face, keyboard-focusable, emitting `ui.action`. Pointer coordinates only if S6 allows. *Exit:* chrome adds no Active-tier time at idle (checked with the face's per-minute log).

Fn+F6 or a spoken request opens the Quick Menu. It accepts keyboard selection and spoken alignment. After explicitly opening or moving the menu, the owner can say “actually, put it on the left” within 15 seconds. Keyboard focus stays with the menu until the requested panel appears or the owner dismisses it. The optional clock and system widgets read existing body events. The weather widget in the later source displays a recent, explicitly requested reading only while Internet mode and web lookup permission are active. Native pointer and pacing checks remain open. The archived cable image's keyboard Lists case still needs debugging.

The native launcher adds the browser prototype's Command+Space shortcut,
Tab for grid or list view, and slash to find an app or a setting under
the same eye dock. Settings results focus the existing controls in Options.
List and search share a fixed-size pane with the selected app's description
or the matching setting's location and the Enter action. Tab and Shift+Tab
move through the Options sections in both directions. The view choice lasts
for the face session. Search text stays in the face process. The exact-image
keyboard path and native pointer checks are still open.

**Phase 5: surfaces.** Memory, Lists, Journal, Mind and Calculator panel renderers (at most two live renderers, dirty rows, 30 fps or less, idle CPU weight); the Monitor processes page and allow-listed restarts; Care controls as registry actions; the "last night" summary. *Exit:* guest mode (`app.privacy`) closes every personal surface; lab tests per app.

Calculator is available as a keyboard-focused panel. Typed expressions and spoken sums use the existing bounded evaluator. Lists, Memory and Journal show bounded read-only pages from their existing stores. Mind shows route counts and recent route IDs from the current process. Monitor adds a process view and allow-listed restarts through the doctor, with a plain yes. Spoken settings links open existing camera, network, backup and update pages. Online Lookup shows the cited plaintext result of an explicit query. The Journal has a Last night page and the face can briefly show a count of completed stages after a long closure. Guest entry closes and clears personal panels. The later source changes are pending verification.

**Phase 6: installer.** Restyle `being-console` screens (S11): Try and Install cards, real hardware rows, a partition bar from `installation_plan()`, the real two-step `ERASE` challenge, byte-driven progress with the GLES awakening, honest power-loss text, eye colour and profile seeded into the new mind, and fixed narration lines. *Exit:* the QEMU install suite passes with the new screens, with no change to `install_apply.py` semantics.

The console now draws the real partition map, advances its header eyes on existing install stages and measured bytes, and offers to keep the chosen Try look. After confirmation it offers fixed local Flite narration of the current stage or outcome when the lid is open, audio is unmuted and no conversation is active. The two-stage confirmation and disk writer keep their original behavior. Native panel review and an audible installer run remain open.

**Phase 7: hardware validation.** On the MacBookPro9,2: frame median and p95, graphics and package power, RC6, temperature, on AC and battery, chrome on and off, and one and two panel renderers. Fold it into the round-2 hardware run. Do not rebuild the frozen first-boot stick (`STATUS.md:13`).

## 6. Budgets the integration must respect
- **CPU:** no new per-frame CPU rasterisation. Chrome textures re-render only on content change. Panel renderers run at idle CPU weight. The face stays near today's 1.9% of one CPU at idle.
- **Frame rate:** chrome, widgets and captions never hold the Active tier. Only motion, speech and transitions earn 60 Hz. The installer scene is capped at 30 Hz, and the Mind illustration at 15 Hz or frozen.
- **GPU and power:** stay at or under today's 1.1-1.4 W graphics power. No backdrop blur and no extra full-screen passes. Bake shadows and gradients into textures when they change. A night tint, if wanted, is a uniform multiply.
- **Battery reads:** never add sysfs polling. Reuse published `body.*` events (each BAT0 read costs 55-64 ms of EC work).
- **Privacy:** no words in `mind.route` or logs. Nothing personal on screen in guest mode. Captions stay off by default.

## 7. Risks
1. The lab detector and the cyan > 0.95 test break with any colour work (mitigated in Phase 0 and by S3).
2. Power and heat: the face is already a suspect for the missing C-states. Chrome makes it worse unless it is change-driven.
3. Merge risk with the uncommitted `crates/face/src/main.rs` (+137 lines) and `link.rs`, and the `mind/` edits. `panel.rs` overlaps only in key handling.
4. Scope creep towards a desktop (S1). Every "window" request should be tested against §12.
5. Docs already disagree with the code (terminal run rule, models partition, `PROTOCOL.md` expression list). Fix them as part of each phase.
6. A cold shader compile slows the first frame. The shader cache lives only in `/run`.
7. Font coverage: no accented letters for names.

## 8. Needs the laptop or you
- GPU time per frame of `eye.frag` at 1280x800, with and without a panel upload, and whether crocus offers a timer query.
- Whether the Calm tier at idle restores deep C-states (the unrun experiment in `findings-1-6.json:34`).
- The per-frame cost of `being-console --panel`, and of two at once.
- Trackpad motion under `kmsdrm`, and whether a visible cursor suits an eyes-first screen.
- Whether a look file can be read before the face's first frame.
- Whether RAM staging during install still fits with the Try cortex loaded, and whether the awakening animation keeps the watchdog fed while the SHA-256 verify runs.
- ~~Your answers to S1-S11.~~ Answered 25 Nov (owner decisions 7 and 24).

## 9. Changes to make in Shell Lab itself (cheap, no product code)
These make the POC an honest design tool and are the bulk of Phase 1:
1. Draw the eyes with the real `eye.frag`/`eye.vert` in WebGL2 (as the website does, with `uOrigin` and `uMarks` kept), the real `Look` triple and the 16 real presets. Map Shell Lab's moods onto them, and remove the voice bars and thinking dots.
2. Dock into the top-centre 256x160 strip over 0.45 s while one surface fills the 1280x640 area below. Show at most one surface at a time, unless S1 changes.
3. Limit the palette to the lab-safe colours, or mark the others as "needs lab change".
4. Installer: real partition sizes from `installation_plan()` logic, the `ERASE /dev/sdX` plus `ERASE MACOS` challenge, the four real stages, honest power-loss text instead of resume, and no `rauc`/`efibootmgr` lines.
5. Firefox: replace it with a text "look it up" panel shown only in Internet mode, or mark it out of scope.
6. Keyboard-first chrome: Tab and arrow focus through the quick menu and widgets, as if the trackpad had no coordinates.
7. Reuse the real phrase list from `docs/PIP-FUNCTIONS.md` for overlapping commands, so the POC and the mind agree on wording.

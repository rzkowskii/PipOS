# Emotion director: design (v2, after adversarial review)

Status: design only, nothing built. Branch `emotion-director` on `7721dd0` (face-expressions merged). Evidence labels: **source** = read in the code at that tip; **host** = measured on the development Mac; **estimate** = reasoning only. No laptop measurement exists for anything here.

v1 was reviewed by three critics against the code (feasibility and latency, honesty and privacy, liveliness). Two blockers and about twenty findings are folded in below; section 11 lists what changed.

## 1. Aim and limits

Pip reacts to what it hears and says, in the moment, with the whole catalogue, and never claims anything untrue about its body.

Coordinator limits: no extra model tokens or calls; the 7 tags stay; nothing between end of speech and first audio waits on the director; zero cost at idle; one arbiter for brainstem-originated expressions; reuse the face lane's guest flag and its two job rows. Anti-goals: a director only as good as the model's tag; twitchy or random-looking reactions; anything spontaneous on screen at night; amplifying a false claim in a reply.

## 2. Facts the design stands on (source)

- Reply emotion rides as `face_cue` on each `say.request` clause (mind/turn.py:4829, 5148, 5262). The voice echoes it **once, on the first audible `say.level` of the clause**, only if volume is above 0 and the speaker is not muted (mind/voice.py:3395-3398; the muted marker is invisible to the brainstem). The hub passes it only from the active voice (main.rs:1047). The face shows it as a speech cue at intensity 0.55, and **any cue, neutral included, clears a held `face.express`** (anim.rs `speech_level`), so ordering matters. A held `face.express` beats the speech cue afterwards.
- `face.express` carries only `name` and `hold`; the face always plays it at intensity 1.0 (link.rs:612-615, anim.rs `express`). `audition_blend` already supports intensity and blend inside the face.
- `mind.emotion` is used three times (missed hearing, camera look, neutral reset). Brainstem `face.express` is created in two places in `machine.rs` (the `mind.emotion` arm and the think-timeout) plus the face lane's two `term.status` rows (`job_face`).
- The face accepts 27 public names at this tip (16 plus the 11 tuned), not 16; more arrive as the face lane promotes them. Unknown names are silently ignored. The face also has its own expression sources (reunion joy, missed-hearing squint, screen concern, battery tired, yawn): these are body-truth exemptions, not director output.
- The brainstem sees the person's words (`hear.final`) and every reply clause (`say.request`) in transit and ignores content today. `Machine::on_event` runs before the event is queued for forwarding (main.rs:1070-1072); the hub itself only does `try_send` into per-organ queues, so the director's cost is CPU on the hub thread.
- Mid-sentence the only signal is `hear.speech_start` then `state.changed listening`. No mic level, no `hear.partial` on the Moonshine path, nothing while Pip is thinking or speaking.
- Guest: `Machine::set_guest` is only refreshed on `term.status` (main.rs:1065-1067); boot and `app.privacy` update only `caption_privacy` (main.rs:725-727, 1025-1029). **As it stands the flag is stale for words.**

## 3. Design

One new module in the brainstem crate, `director.rs`, with a table at `crates/brainstem/data/emotion-director.json` (embedded with `include_str!`; never from `docs/`; validated in a unit test, no runtime fallback, no new crates). All brainstem-originated expressions go through one private method `Machine::show(...)`, which replaces every `Event::new("face.express")` site (the two existing arms and the two job rows). It is a pure function of event, machine state, table and a small RAM-only state, so it replays with fake time like the rest of `Machine`.

### 3.1 Signals and weights

| Weight | Source | How it arrives |
|---|---|---|
| veto | Body truth and quiet | machine state and flags, guest, `announcing`, night/dark with an untrusted clock counting as quiet, `Drowsy`, `muted`, `unwell`, lid, Effects (from `screen.prefs_state`) |
| 4 | Heard words | a `mind.emotion` from the mind, sent only after it has accepted the turn |
| 3 | Reply clause words | read in the brainstem from Pip's own `say.request` text |
| 2 | Event meaning | table rows: job failed or done, think-timeout, camera look, missed hearing |
| 0.5 | Model tag | the clause's `face_cue`; `neutral` counts 0. Measured on 42 strict replies (Research desk, host): neutral 34, joy 4, curiosity 4, fit about 1.5 of 2, "rarely wrong, mostly flat" |
| small | Drives | `body.drives` (arousal, social, tiredness) nudge the choice |

### 3.2 Blend rule

Each cue names a **family** and a strength 0..1. A family is a short ladder of **visibly distinct anchors**, not many near-identical poses: warm (pleased, joy, excited), doubt (skepticism, smug, annoyed), worry (concern, sad, disappointed), surprise (surprise, gasp, shocked), humour (amused, giggle, laugh), calm (neutral, bored), and the rest from the groups sheet. The director sums weight x strength per family, picks the winner and maps strength to an anchor and an intensity (0.4 to 1.0), so mild feeling is the same anchor at lower intensity. Eyes-only poses are close together (brows, mouth and effects are off by default), so a rung is added only if its eyes-only form is clearly distinct. The groups sheet gets `family` and `rung` columns, and the tests produce a reachability report (for each of the 89: group, family, live or waiting for promotion).

A rung's pose must be live in the face (27 names today). Until the face lane promotes the warm, doubt and surprise anchors the ladder collapses to what exists; that is expected, not a bug.

### 3.3 Behaviour over a turn

1. **Person finishes (`hear.final`).** The mind's cue arrives as `mind.emotion` while Pip is Thinking. Show it about 1.0 to 1.4 s so it overlaps the face's own look-away and ends as the thinking pose takes over; skip it if the speech age is already over about 1.2 s. For tone, prefer **gaze** where it fits: a short down-and-aside glance for sadness or apology, held eye contact for about a second for thanks, using `face.gaze`, which exists.
2. **Reply, first clause.** Show the reply's cue family at the first `say.request` while still Thinking (it leads the voice), at intensity 0.55 with a long hold; the face already clears it on reply end, stop, cancel and mute. Later clauses shift only when the family changes, keyed on the voice's audibility-gated echo (next item).
3. **Reply, per clause.** The director sees each clause's text in `say.request` and queues (reply, seq, cue) in a bounded FIFO, cleared on turn end, `say.stop`, `key.interrupt` and organ loss. When the first `say.level` carrying `face_cue` for that clause passes the hub, the director's `face.express` is queued **after** it in the same batch: the echo clears the held pose first, then the director's pose lands and wins. It emits only when it **disagrees with or adds to** the tag, so the common case adds zero events. `face_cue` is never stripped. A clause that is private, an announcement or a camera description is skipped, so it keeps the face's ordinary cue.
4. **Afterglow.** On a completed, uninterrupted reply, hold a low-intensity (0.4 to 0.55) version of the closing pose for 0.8 to 1.2 s, then let it fall to attentive. Today it snaps off.
5. **Dwell.** At most one director pose change per about 1.2 to 1.5 s, at most 3 per turn, no startle-kick pose (surprise, startled, scared, shocked) twice within 10 s. Variety comes from hold, intensity (0.45 to 0.65 around the cue), 60 to 180 ms entrance jitter and gaze side, not from swapping pose names.

### 3.4 Mid-sentence: today and later

Today only `hear.speech_start`: the face already leans in. The director adds **nothing timer-driven** that a person would read as agreement (no nods, no brow lifts): a nod sent blind is a false statement. If a gaze move is wanted it is one small re-fixation per utterance after about 2.5 s, cancelled on `hear.final` and `hear.cancel`. Genuine mid-sentence reaction needs the requests in section 7.

### 3.5 Mood

A slow RAM-only state, never persisted, never in a snapshot, `state.changed` or log, reset to neutral on guest change, lid, sleep, forget commands and mind reconnect. Its effect is capped at one rung. The director does **not** hold baseline poses or generate idle life: the face already has idle behaviour and the honest low-battery cue, and a held pose would suppress both. If a baseline lift is wanted it should be a slow `valence` drive that the face applies as a tiny blend (phase 4, optional).

## 4. Who reads which words (decision for the coordinator)

Reading Richard's words in the brainstem is not forbidden by PROTOCOL.md, but the brainstem cannot decide the skip rules (accepted turn, `followup`, guest, private, speech not meant for Pip), the guest flag is stale for words, and the mind already knows all of it.

**Proposed split:**
- **Pip's own clauses** (`say.request`): read in the brainstem, stateless, in transit. The event carries `private`, `announcement` and camera markers to skip on, and the text is Pip's, not the owner's. Cost: a few microseconds on the laptop (estimate; the Python equivalent measured 3.8 us per 28-word clause on the host); the scanned text is capped.
- **The person's words** (`hear.final`): the mind computes the cue after it has accepted the turn and sends the existing `mind.emotion`. No new topic and no words in the brainstem. The Conversation lane adds a small call site; I write the lexicon module and its tests as new files (`mind/affect.py` plus data) so the lane only wires it in. Fallback if that lane cannot take it yet: the brainstem reads `hear.final` only once the guest flag is fixed and when `followup` is false, using the same table.

**One text mechanism.** The director's lexicon includes the two `clause_cue` patterns (`mind/speech.py:10-22`) as rows. `clause_cue` stays untouched for now; the mind's concern then counts only as the 0.5 tag vote, so it is not double-counted. After the director works, the Conversation lane can delete `_CLAUSE_CONCERN` in one change (pins in `tests/test_mind.py:1027-1032` move to the table tests).

**Precision first.** A wrong reaction is worse than none. The first heard-words reader is a short, high-precision whitelist: greeting, thanks, goodnight, plain statements of sadness or loss, laughter words, an apology aimed at Pip, a joke. It is additive and asymmetric: text never flips a positive tag to a negative one. A broader lexicon waits for a measured precision floor.

**No false claims.** Words map only to social families. "I see", "I hear", "I feel hot", "I looked" never map to a sense or body pose; those follow real bus state only.

## 5. Honest body, quiet, state gating

Groups (sheet: `emotion-director-groups.json`): **a** any time (35); **a\*** (20) the same, a drawn mark only while Effects is on; **b** (22) poses tied to a body or state condition; **c1** (8: love, adoring, flirty, crying, heartbroken, raging, freezing, ko) unlocked by the owner's answer (row 25 in `docs/DECISIONS-OWNER.md`, relayed 28 Nov), only with Effects on, with eyes-only stand-ins otherwise (pleased, smug, sad, angry; freezing and ko unused); **c0** (4: sick, queasy, p_sneeze, p_hiccup) lab-only.

- **Group b is never emitted by the director.** The face already derives listening, thinking, attentive, muted, asleep, sleepy, tired, low-battery and wake poses from state, and a held `face.express` could outlive the state it claims. Poses with no face owner (searching, scan, success, sound, music, sing) wait for a real bus signal and a later, separate decision. Several of the sheet's conditions are not real signals today, so the sheet is information, not gating.
- **Marks are the face lane's job** (agreed): drawn from a data allow-list, all stripped when Effects is off, flame and steam suppressed while a fresh `stem.budget` says hot or critical, X and spiral pupils suppressed while unwell, link-lost or loading, with a short settle. The guards apply per mark channel, not per pose list. The director sends a name, a hold and an intensity. It reads `screen.prefs_state {effects}` (look.rs:125) to choose between a c1 pose and its stand-in; unknown means off.
- **Guest is fixed first.** The flag is set in the `app.privacy` arm and at boot (main.rs), and unknown is treated as guest. Test: guest toggled through `app.privacy`, then `hear.final`: no reaction.
- **Quiet at night.** Spontaneous output (anything that is not a direct reply to an accepted turn) is off when the machine is night or dark, when the clock is untrusted, or when the state is Drowsy or Asleep. The missed-hearing concern and wake reactions without an accepted `hear.final` count as spontaneous.

| State or flag | The director may show |
|---|---|
| Asleep, Waking, lid blocked, muted, unwell | nothing (the face's own poses own the face) |
| Drowsy | nothing spontaneous |
| guest | state poses and job rows only, no word reactions |
| Listening | nothing of its own; the face leans |
| Thinking | the heard-words cue and the first-clause lead |
| Speaking | reply-clause cues, afterglow |
| Idle or Attending, day | job rows, and replies to accepted turns only |

## 6. Cost and latency

The director's work is bounded text scanning (capped) plus table lookups on the hub thread. It cannot reorder the forward of the triggering event: the order in the queue is the original event, then `state.changed`, then the director's `face.express`. The budget is stated as a **measured per-event time** (host micro-benchmark, recorded with its label), not "never". Idle cost is one check inside the existing `on_tick`.

## 7. Requests and new messages

- **Face lane (one small change, about 10 to 12 lines in link.rs):** `face.express` optional `intensity` and `blend` (the face already has them inside); `face.release` to end a held pose early; `face.gesture {name}` for nod, shake and content (head gestures are already guarded for muted, dazed and wake). Intensity is **required** for reply cues, so they match the speech cue's 0.55; the rest allow agreement nods and refusals as replies.
- **Conversation lane:** a call site for the heard-words cue after the turn is accepted; the `_CLAUSE_CONCERN` cleanup later; `mind.emotion` gains an optional `intensity` once the face accepts it.
- **Voice lane, later:** `hear.level` (numbers only, 10 to 25 Hz) and an early `hear.speech_end`; neither exists. They are what would allow genuine mid-sentence reaction and a faster pre-reply moment. Behind latency Stage 1.
- **Protocol docs:** `screen.prefs_state` should be listed in PROTOCOL.md.
- **Mine, after rebase:** the guest refresh in `main.rs`; the `show()` arbiter; the table and tests.

## 8. Test plan (host, deterministic, synthetic only)

Fake time through `Machine::on_event` and `on_tick`; fixtures are synthetic JSONL (`{"t","topic",...}`) from the persona probes, the stored audition triples, the pinned expectations in `tests/test_mind.py` and the l4 timeline for ordering. No real owner speech.

1. **Safety properties** over every fixture and a randomised walk through states and flags: never a c0 pose; never a c1 pose with Effects off or unknown; no director output while asleep, waking, muted, unwell or lid blocked; no word reaction in guest; no spontaneous output at night, with an untrusted clock or while drowsy; no group b pose emitted.
2. **Ordering**, in a main-level test: the triggering event precedes the director's output; the director's `face.express` follows the voice echo in the same batch; `face_cue` is never removed.
3. **Clause FIFO:** reset on turn end, `say.stop`, `key.interrupt` and organ loss; a skipped or rejected clause cannot desync later clauses; volume zero or a muted speaker produces no director output.
4. **Cue quality:** about 200 synthetic lines with expected family and a precision floor; the tag outvoted by clear text and kept when the text is neutral; no positive-to-negative flip.
5. **Dwell, variety, afterglow, mood reset** (guest change, lid, sleep, forget).
6. **Reachability report** for the 89 (group, family, live or waiting).
7. **Cost:** host micro-benchmark per clause and per `hear.final`, recorded with its label.
8. **Existing tests stay green:** `machine.rs` tests, the face lane's job rows, `hub.rs`.

Real-laptop checks (3 s to first audio, under 95 degrees C, how it looks on the panel) go through the Device lane after the milestone image; nothing here claims them.

## 9. Phases

0. (now, new files only) table, pure functions, lexicon, tests, groups sheet columns.
1. After approval and rebase: guest refresh, `Machine::show()` replacing the four emit sites, dwell rule, afterglow.
2. Reply-clause cues with the echo-keyed FIFO and the first-clause lead.
3. Heard-words reaction (mind side) and gaze tone.
4. Face requests landed (intensity, release, gesture): agreement and refusal gestures; mood and valence.
5. Voice-lane requests when Stage 1 is done.

## 10. Decisions and questions for the coordinator

1. The text split in section 4 (heard words in the mind, Pip's clauses in the brainstem), against reading everything in the brainstem.
2. May I fix the guest refresh in `main.rs` after the rebase (a few lines in the `app.privacy` arm and at boot)? The face lane's hook is the right flag, but it needs feeding for words.
3. May the face lane's requests in section 7 be taken as one change, with `intensity` first?
4. Which lane takes the face-local sources (reunion joy, missed squint, screen concern, battery tired)? I list them as body-truth exemptions. The missed-hearing squint and the mind's missed-hearing concern should become one, not two.

## 11. What changed from v1 (from the review)

Guest flag fixed instead of assumed. `face_cue` no longer stripped: the voice's echo is the trigger, since it carries the audibility gate, and the director's pose follows it in the same batch because the echo clears held poses. Intensity needed, not optional. 27 names, and ladders depend on promotions. Group b and the marks machinery removed from the director. Backchannel nods and idle life cut (a blind nod is a false statement). Heard words move to the mind. Clause length treated as unknown (long hold, the face clears on end). Dwell and entrance limits added. Afterglow, first-clause lead and gaze-as-tone added. Mood restricted to RAM with reset rules. Night rule fails closed. The "after the route" claim replaced by an ordering guarantee and a measured budget.

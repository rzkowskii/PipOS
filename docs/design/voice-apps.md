# Voice apps: Pip's terminal showcase

Status: design only, 23 November 2026. Nothing here is built, and every timing
is an estimate until it is measured on the MacBook. It combines four studies
(screen and terminal, voice commands, safety and resources, code generation on
small models) into one plan. Where they disagreed, section 2 gives the call and
the reason.

The owner asked for this:

> As a proof of concept (As I know the target hardware is constrained), I want
> it to have voice commands to open and interact with a select few very simple
> applications. Example: "Hey Pip, open the Terminal" and a terminal will open.
> Hey Pip, do the following in the terminal (Tell Pip to install an application
> or write a script) and then the AI will do it. More for showcase. Obviously I
> don't want to bog the hardware down. "Hey Pip, close the terminal" "Hey Pip,
> minimise the terminal" "Hey Pip, stop writing that script for a second"

## 0. The design in brief

- **The face keeps the screen.** When an app opens, Pip's eyes glide up into a
  strip at the top. The app fills the area below. There is no window system and
  no black screen between the face and the app. The Try/installer menu keeps its
  current path and does not change.
- **A new organ, "hands", does the work.** It holds the terminal session, types
  into it, writes the code, runs it and reports back. The mind matches the
  commands in code, says the short lines, and decides who gets the model.
- **The shell is caged.** It runs as an unprivileged user in a RAM-only
  workspace, with no network, no access to Pip's memory and no way onto the bus.
  It gets only CPU that nothing else wants.
- **Most showcase code is pre-written.** Common requests use tested "recipes"
  from Pip's recipe book, and the screen says so. The 0.8B model writes the
  rest, held to a strict output grammar. Nothing runs until you say "run it".
- **"Install" is honest.** Pip carries a small shelf of tiny programs and
  installs those. It cannot download anything, and it says so.
- **Nothing new runs until it is used.** The apps cost nothing until one is
  opened. Code writing never holds the "thinking" state, so the wake phrase
  stays live and "Hey Pip, stop" is always heard.

## 1. What the owner sees and says

### A full session

Times are estimates for the MacBook, counted from when you stop speaking.

| # | You say or do | You see | Pip says | Time |
|---|---|---|---|---|
| 1 | "Hey Pip, open the terminal." | Pip's eyes glide up into a 160 px strip and shrink. A terminal rises into the space below and shows the prompt `pip:~$`. The status bar reads "Ready". | "Here's my terminal." | Panel up in about 1.5-3 s |
| 2 | "Hey Pip, write a script that draws a spiral." | Pip types `# you asked: write a script that draws a spiral`, then `# recipe: spiral (from Pip's recipe book)`, then `cat > spiral.py <<'PIP_END_3f9a'` and the 13 lines of the script. Its eyes follow the cursor. | "I know this one." | Typing starts within 1-2 s and runs about 12 s |
| 3 | Half-way through: "Hey Pip..." | Typing stops as soon as the wake phrase is heard. The eyes look up at you. | | About 0.5 s after "Pip" |
| 4 | "...stop writing that script for a second." | Status: "Paused · say carry on". | "Paused." | About 1 s |
| 5 | "Hey Pip, carry on." | Typing picks up at the same character. | "Carrying on." | Instant for a recipe. For model-written code, see section 7.6 |
| 6 | (Pip finishes typing) | The end line `PIP_END_3f9a` is typed and the file is checked. Status: "Written · say run it". | "It's ready. Say run it." | |
| 7 | "Hey Pip, run it." | Pip types `python3 spiral.py` and presses Enter. The spiral is drawn. Status: "Running 0:01", then "Done · exit 0". The eyes brighten. | "Running it." Then: "It ran without errors." | First output in under 1 s |
| 8 | "Hey Pip, minimise the terminal." | The panel slides away and the eyes grow back into the full face. The session keeps running. | "Okay." | 0.45 s |
| 9 | "Hey Pip, show me the terminal." | The panel comes back exactly as it was. | "Here it is." | 0.45 s |
| 10 | "Hey Pip, close the terminal." | The shell is stopped and the panel goes. spiral.py stays in the workshop until the machine restarts. | "Closed." | Up to 3 s |

Optional beats for the same demo:

- "Hey Pip, write it yourself: a script that counts down from ten." The words
  "write it yourself" skip the recipe book. The screen says
  `# written by my language model (Qwen3.5 0.8B)`. The script appears in about
  3.5-8.5 s. Pip says "I'll try." and never claims it works, only that it ran
  without errors.
- "Hey Pip, in the terminal, show me the files." Pip types `ls -l`. The status
  bar counts down: "Running in 4 · Esc or say stop". Then Pip presses Enter.
- "Hey Pip, install snake." Pip types `install snake`. The helper prints that
  snake comes from Pip's built-in shelf and that no internet was used. Then
  "Hey Pip, run snake", and you play it with W, A, S and D.
- "Hey Pip, show me how you're feeling." This opens the body monitor:
  temperature, load, memory and battery. "Hey Pip, show me the clock" opens the
  clock with your timers.
- Esc at any moment stops whatever is innermost: Pip's speech first, then
  Pip's typing, then a running program.

### What it needs from you

- Until task 23 lands, say "Hey Pip", wait for the eyes to look up, then say the
  command. Today's ears drop the wake phrase if it runs straight into the
  command (section 3.7). After task 23, "Hey Pip, open the terminal" works in
  one breath, but only for commands.
- Pip asks before anything it can't undo: closing while something is running,
  and (later) deleting files it did not make.
- Rehearse the demo sentences. Recipes and a fixed seed make the same sentence
  give the same script every time.

## 2. Decisions: how the four studies were combined

| Question | What the studies proposed | The call | Reason |
|---|---|---|---|
| Who owns the screen | Face composites a panel (screen); the others assumed a console of its own | The face composites one panel; being-console renders the app off-screen into a frame file | No black gaps, the eyes stay alive, and the face keeps its bus role, so the being never looks unwell |
| Who writes the code | mind/tasks.py (voice); the terminal process (codegen) | The hands organ | Script text, keystrokes and output never cross the bus, and the job lives and dies with the session |
| New brainstem state "working" | A new state (voice); an overlay (safety) | No brainstem change | `idle` already keeps the mic on the wake phrase only. The voice, eyes, camera and face all key on state names and would need updating for a new one. hands reads `state.changed`, `body.lid`, `key.interrupt` and `hear.wake` itself |
| Typing pace | 16 characters a second (screen); model speed (voice, codegen) | The model's own rate through a jitter buffer; recipes at about 30 characters a second | The model already writes at about typing speed. Slowing it down only adds waiting |
| Where files live | Loop image on /mind (screen); /mind/workshop (codegen); RAM (safety) | A 64 MB RAM-only tmpfs at /workshop, mounted noexec | Real forgetting. It can never fill /mind, and /mind is noexec anyway |
| "Install an application" | A script in ~/bin (screen); a shelf (safety); mini-app recipes (codegen); a refusal (voice) | A built-in shelf plus an honest "I can't download" | It is the only option that is both offline and impressive. A script in ~/bin cannot be executed from a noexec workspace |
| When code runs | Confirm first (screen); "run it" (voice); 4-second countdown (safety) | Scripts and generated one-line commands wait for "run it". Built-in one-line recipes have a 4-second countdown you can stop | Confirm code Pip wrote. The recipe countdown gives the owner time to catch a mishearing |
| Cage limits | 512M/64 tasks (screen); 256M/48 (safety); 128M/16 (codegen) | Safety's numbers: 256 MB, 48 tasks, half of one logical CPU, idle scheduling | Safety checked the kernel. Section 5 says honestly which limits are inert until the next image |
| Lid closed | Jobs keep running (screen); freeze (safety) | Freeze the cage, thaw on open | A closed lid means nobody is watching. A 13-inch chassis in a bag must not heat up |
| A bare "stop" | Pause (voice); stop the run (codegen) | Pause, in every phase. Esc interrupts a running program | A misheard "stop" must never destroy work. "Cancel" is the word that ends it |
| How pause works | Hold the display or park the generation | Hold on the wake phrase. Park on an explicit pause and whenever a conversation turn needs the model | A held display looks up at once. Parking frees the CPU. Gate M4 can switch an explicit pause to hold if resuming proves slow |
| Sampling | Greedy (voice); temperature 0.2 with a seed (codegen) | Temperature 0.2 with a seed taken from the request | Greedy decoding loops more on small models. The text after a pause differs from an unpaused run, but nobody ever sees the other version |
| Fix attempts | One automatic model fix (codegen) | The missing-import fix is automatic and typed on screen. A model fix happens only when you say "fix it" | Each model fix roughly doubles the wait, so you decide |
| Bus topics | app.request/app.shown (voice); app.*/term.* (screen); code.* (codegen) | `app.*` and `term.*` only | One vocabulary |
| Key path | Face → being-console socket (screen) | Face → hands socket. The renderer takes no input | One place decides who is typing |
| Renderer privileges | Child of a root organ (screen) | being-console in panel mode runs as an unprivileged user, being-panel | It parses bytes that caged programs write. A parser bug in C must not become root |
| Other apps | Body monitor, clock and file viewer (screen) | Body monitor and clock | `cat` in the terminal already views files. Three apps is the owner's "select few" |
| Voice upgrades | Grammar-first search plus one-breath (voice) | One-breath in the core plan. The control grammar is optional, after measurement | The owner's own phrasing needs one-breath. The grammar only pays off if measured accuracy is poor |
| Time limit for Pip's runs | 20 s (codegen); 300 s (safety) | 60 s for commands Pip starts | Showcase scripts are short. A forgotten `while True` stops within a minute |
| Model slots | Two slots (all three) | Two slots, after measurement M3 | Writing code must never evict the conversation's cached prompt |
| Coder model | Optional, after an audition (codegen) | Same | It might be better, but it costs a second signed model pack |

## 3. Architecture

### 3.1 Processes

```
 mic ── voice ─────────── hear.wake / hear.final ───────────┐
                                                            ▼
 keyboard ── face (KMS master) ── key.interrupt ──► brainstem (bus hub, state)
              ▲   │                                         │ state.changed, body.lid,
              │   │ key bytes (datagrams)                   │ hear.wake, key.interrupt
              │   ▼                                         ▼
  reads ──────┤  /run/being-hands/keys ◄──── hands (root, Python) ◄── app.* / term.* ── mind
  /run/being- │                                │   │   │                                │
  panel/*.frame                                │   │   └─ llama-server slot 1 (code)    └─ slot 0 (talk)
              └── being-console --panel ◄──────┘   │
                  (user being-panel, headless,     └─ pty ◄──► /bin/sh as being-shell
                   renders bytes into the frame)        (transient unit: the cage)
```

| Process | Runs as | What it does |
|---|---|---|
| being-face (existing) | root, KMS master | Draws the eyes as today. Gains a one-panel compositor (`crates/face/src/panel.rs`) and forwards keys to hands while a panel has focus |
| being-hands (new, `mind/hands.py`) | root, Nice 10, bus name `hands` | App state machine, terminal session, typist, recipes, code generation on slot 1, checks, runs, limits, forgetting. Starts only when apps are switched on (section 5.6) |
| being-console `--panel` (new mode) | being-panel, in its own hardened transient unit | Renders one app's bytes into `/run/being-panel/NAME.frame`. No SDL video, no DRM, no keyboard. At most two run at once: the terminal and one other app |
| being-shell-N (transient unit) | being-shell | The caged `/bin/sh -i` (BusyBox ash) on a pty that hands holds |
| mind (existing) | root | Matches app commands, speaks the lines, takes the model for conversation (parking the code job), announces "It's ready", builds spoken reports from facts |
| llama-server (existing) | root, Nice 5 | Two slots after measurement: slot 0 for conversation, slot 1 for code |

The Try/installer menu (being-local-screen.service, mind/local_screen.py, the KMS
path of being-console) is unchanged. Panel mode is a separate code path chosen by
`--panel`. Without it being-console behaves byte for byte as now, and
`test_local_screen` must still pass 19 of 19.

### 3.2 Screen model (1280x800)

- **Full face:** as today.
- **With a panel shown:**
  - The top 160 px is the eye strip. The eye shader draws into a 256x160
    viewport centred at the top. That keeps 16:10, so the eye geometry does not
    change.
  - The lower 1280x640 is the panel: one textured quad of the frame file.
  - The panel draws its own chrome:
    - a 28 px status bar, with the app title on the left and Pip's status on
      the right ("● Pip typing", "Paused · say carry on", "Running 0:12 · Esc
      stops", "Running in 4 · Esc or say stop", "Done · exit 0");
    - a 40 px footer: Esc Stop Pip · F3 Hide · F10 Mic · PgUp/PgDn.
  - In the medium 11x24 font that leaves a grid of about 107 columns by 23 rows.
  - Program output can never draw over the chrome. The countdown and the status
    therefore cannot be faked by a script.
- **Transitions:** 450 ms smoothstep. Opening shrinks the eyes into the strip
  while the panel fades up. Minimise and close play it in reverse.
- **The face covers the panel, without changing app state, when:**
  - Pip is asleep (the full-screen closed eyes);
  - the lid is closed (blank);
  - the Try menu holds the screen (the face is stopped). F2 still works with a
    panel open, and the panel comes back when the menu exits.
- **The face shows a panel only when:**
  - its header says visible;
  - its beat changed in the last 2.5 s, so a dead renderer is hidden, not
    frozen on screen;
  - the header is valid.
- **Cost when unused:** with no frame file, the face looks at the directory once
  a second.

**Frame file** (`/run/being-panel/NAME.frame`, written only by its renderer;
format in a new `docs/PANEL.md`):

- A 128-byte header. It starts with the magic "BPN1", then size, stride,
  sequence, dirty rows, flags, cursor position, beat and a 32-byte title.
- Flags: visible, focus, pip_typing, pip_paused, job_busy, human_typing,
  scrolled.
- RGBA rows follow the header.
- Seqlock:
  - The renderer writes an odd sequence, then the dirty rows, then an even
    sequence.
  - The face reads the header, then the dirty rows, then the sequence again. On
    a mismatch it drops that frame.
  - The face uploads only the dirty rows with TexSubImage2D. Rust std is enough
    (`FileExt::read_at`).

**Renderer in panel mode:**

- It blocks in `poll()` on its input and a signal pipe. While hidden it sleeps
  with no timeout.
- It draws at most 30 frames a second, and only dirty rows. Today's console
  redraws all of 1280x800 at up to 50 Hz.
- It bumps the beat every 500 ms.
- It takes flags from hands in-band as OSC 7770 (`pip=typing|paused|idle`,
  `job=busy|idle`, `human=1`, `title=`, `wipe=1`).
- Its scrollback can be wiped for real. That is needed for forgetting; today
  ED 2J/3J only pushes lines into the 400-line history.

**Eyes in strip mode** are decided in the face from the header flags. No bus
traffic is needed.

- Priority: asleep or blank > listening (someone is talking to Pip: look at
  them) > the app focus below > camera presence > idle wander.
- pip_typing: look at the cursor, with small reading saccades 80-160 ms after a
  jump. Blink more slowly, and blink once after each Enter.
- job_busy: watch the newest output line.
- pip_paused: look up at the person and hold.
- On a finished run, the mind sends `mind.emotion`: joy for 1.5 s on exit 0,
  concern for 2 s otherwise.
- With the terminal minimised, the flags still arrive. The full face then
  suppresses idle performances (survey, content) while Pip types.

### 3.3 Keys

- **While a panel is shown and focused:**
  - The face translates SDL keys with a Rust port of being-console's
    `key_character()`. That covers printable keys, Enter, Backspace, Tab,
    Ctrl+letter, the arrows, Home, End and Delete (US layout only).
  - It sends one datagram per key to `/run/being-hands/keys`. Sends are
    non-blocking, and a full socket drops the key.
  - hands decides what each key means. PgUp/PgDn scroll the renderer, F3
    hides or shows the terminal, and other keys are your typing into the shell.
- **Esc** stays `key.interrupt` on the bus and never reaches the shell.
- **Ctrl-C** reaches the shell as byte 0x03. That is new: today's console drops
  every Ctrl key.
- **M** no longer mutes while a panel has focus, because it is a letter. F10
  still mutes. F9 and F2 are unchanged.
- **Your typing while Pip is typing** pauses Pip (`term.status paused
  reason=human`). When Pip resumes a command line, it first clears the line
  with Ctrl-U and retypes it.
- uinput is not in the laptop kernel. Pip "types" by writing to the pty master,
  which is direct and cheaper.

### 3.4 Control channel: bus messages

These go into `docs/PROTOCOL.md`. Keystrokes, frames, script text and program
output never cross the bus. The bus carries only your request text (already on
the bus as `hear.final`), short status, and, only when you ask "what did it
say?", one output line of at most 80 characters.

| Topic | From → to | Fields | Meaning |
|---|---|---|---|
| `app.open` | mind → hands | `app` (terminal, monitor, clock), `req` | Open, or restore if minimised |
| `app.close` | mind → hands | `app`, `force`, `req` | Close. Refused with `busy` if something is running, unless `force` |
| `app.minimise` / `app.restore` | mind → hands | `app`, `req` | Hide or show. Minimise closes the two small apps |
| `app.state` | hands → all | `app`, `view` (shown, minimised, closed), `session`, `job` (phase or null), `req` | Sent on every change, and again when `organ.up` names the mind |
| `app.refused` | hands → mind | `req`, `reason` (disabled, busy, no_session, lid_closed) | The request could not be done |
| `app.privacy` | mind → hands | `guest` | Guest mode changed: close the session and clear the workshop |
| `term.task` | mind → hands | `id`, `kind` (recipe, script, command, install, run_app), `text`, `recipe`, `slots`, `force_model` | Do this in the terminal |
| `term.control` | mind → hands | `id`, `action` (hold, release, pause, resume, cancel, undo, run, fix, stop_run), `reason` (owner, turn, wake, key, lid, sleep) | Control the job |
| `term.status` | hands → mind | `id`, `phase`, `source` (recipe, model, command, shelf), `file`, `lines`, `seconds`, `exit`, `reason`, `speakable` | A phase changed (section 3.5). At most every 2 s while writing |
| `term.read` / `term.text` | mind ↔ hands | `id`, `what` (last_line) / `text`, `lines` | Only when you ask what it printed |
| `term.forget` | mind → hands | `id` or `all` | Wipe a job everywhere (section 8) |

hands also subscribes to:

- `state.changed`, `body.lid`, `organ.up` and `organ.down`;
- `hear.wake`, so it can hold the display at once;
- `key.interrupt`.

The mind adds `app.state`, `app.refused`, `term.status`, `term.text`,
`organ.up` and `organ.down` to its subscriptions. The mind waits up to 2 s for
`app.state` or `app.refused`, then says what actually happened. Pip claims
"Here's my terminal" only after `app.state` reports the panel shown. With no
answer it says "My terminal didn't open."

No brainstem change is needed. `key.interrupt` is routed to every subscriber
before the brainstem's own state change. So when hands sees Esc with its last
state still `speaking`, it knows the brainstem is stopping speech and leaves
the job alone. That is what makes Esc "innermost first".

### 3.5 State machines

**Apps** (in hands; at most one app shown at a time):

| From | Event | To |
|---|---|---|
| closed | open | shown (the terminal starts a shell and its renderer) |
| shown | minimise | minimised. The terminal keeps running and Pip keeps typing, without animation. The small apps close instead |
| minimised | restore or open | shown, redrawn exactly from the renderer's grid |
| shown or minimised | close | closed, if no job is active. Otherwise refused with `busy`, and the mind asks "Something is still running. Close it anyway?" |
| any | another app opens | The terminal is minimised. A second small app replaces the first |
| any | hands restarts | closed. It stops any leftover `being-shell-*` units and reports honestly |

**Terminal jobs** (one at a time):

| Phase | Means | Leaves by |
|---|---|---|
| reading | The model is reading the request (status "reading your request… 3 s") | writing, paused, cancelled, failed |
| writing | Text is arriving and Pip is typing it | held, paused, checking, cancelled, failed |
| held | The display has stopped (you said "Hey Pip"), but generation continues into the buffer | writing on a release, after 6 s with no speech, or when the state returns to idle; paused on a pause command or a conversation turn |
| paused | Typing has stopped and the generation is parked: the stream is cut and its token IDs kept | reading or writing on resume, cancelled |
| checking | The file is complete. The syntax, policy and free import fix run | waiting, failed |
| waiting | A script or generated command is ready. "It's ready. Say run it." | running ("run it"), cancelled |
| countdown | A built-in one-line recipe is ready, 4 s before execution | running, or cancelled (Esc, "stop": Ctrl-U clears the line) |
| running | The program is in the foreground (tcgetpgrp shows it) | done, failed, frozen ("stop for a second"), stopped (Esc, "cancel", 60 s limit) |
| frozen | The session cgroup is frozen | running on "carry on", stopped on "cancel" |
| done / failed / cancelled | The end | A cancel can be undone for 30 s with "carry on" or "undo that" |

Finished detection:

- The prompt carries an invisible marker, OSC 7771 with `$?`, set in PS1.
- hands believes the marker only while the shell itself is in the foreground
  (`tcgetpgrp`). Programs cannot forge it, because hands strips OSC 7770 and
  7771 from everything the shell side writes.
- If BusyBox ash cannot expand `$?` in PS1, the exit status is reported as
  unknown and only `tcgetpgrp` is used.

### 3.6 Esc, lid, sleep and the mic

- **Esc:**
  - If the being is thinking or speaking, the brainstem stops speech as today,
    and hands does nothing.
  - Otherwise, during reading or writing it pauses. During a countdown it
    cancels the run. During running it sends Ctrl-C, then, after 2 s, kills
    every process in the session's cgroup except the shell.
- **Lid closed or asleep:** typing and generation pause, and the cage is frozen
  (`systemctl freeze`; the cgroup v2 freezer needs no kernel option). On lid
  open the cage is thawed, and Pip says "I paused while the lid was closed. Say
  carry on."
- **Heat and battery** (guesses until measured): freeze at 90 °C or more on the
  hottest core, thaw below 80 °C. Refuse new runs when unplugged at 15 % or less.
- **The mic:**
  - Code writing never puts the brainstem in thinking. After "Okay, I'll write
    that" the being lands in attending, then idle, where the mic listens for
    the wake phrase only. That costs almost nothing, and Pip is silent while
    typing, so there is no echo.
  - This matters because thinking closes the mic and times out after 120 s.
- **Cortex lease:**
  - Before a conversation turn that needs the model, while a job is reading or
    writing, the mind sends `term.control pause reason=turn`. It waits up to
    0.5 s for `paused`, answers on slot 0, then sends `resume`.
  - Commands answered in code never take the model from the job.
  - While a job holds the model, the mind also skips read-ahead, compaction and
    re-warming. They use slot 0 and would halve both speeds.
  - A model turn during a job gets a short note, such as "(You are writing a
    script in the terminal: a spiral.)".

### 3.7 Voice

What the voice does today:

- Dictation uses the generic en-us language model.
- The wake phrase is accepted only if the speech segment ends within 0.50 s of
  "Pip".
- The endpointer needs about 0.36 s of quiet to close a segment.

So "Hey Pip, open the terminal" said in one breath is dropped today. The mind's
`commands.parse("forget that script")` returns the memory forget command.

The plan, in order:

1. **Phase 1 (core):** a new `mind/app_commands.py` matches the commands in
   code (section 4), working on today's transcripts. Its start words go into
   `commands._COMMAND_START`, so read-ahead never starts on them.
2. **Phase 2 (core, task 23): one breath.**
   - The recognizer keeps the segment from its onset.
   - If "hey pip" matched at the start but the tail is longer than 0.5 s, it
     decodes the rest and sends `hear.wake {oneshot}` then
     `hear.final {oneshot}`.
   - The mind acts on a one-breath final only if it parses as an app command or
     one of the harmless commands (time, date, battery, name, sleep). Anything
     else is dropped and not recorded.
   - The 0.5 s rule for conversation does not change, so mid-sentence false
     wakes stay out.
3. **Phase 3 (optional, task 24): a control grammar.**
   - A JSGF grammar with a garbage loop is searched first while a terminal is
     open. The language model re-decodes the audio if the grammar rejects it.
   - Only if measurement M6 shows command accuracy is poor.
   - The grammar may use only words that are in cmudict, so American spellings
     such as "minimize" are needed.

## 4. Commands

The mind matches these in code, after normalising the transcript. It strips
"hey pip", "please", "can you", "go ahead and" and fillers, and turns spoken
numbers into digits. The first match wins, and the whole utterance must match.

| Command | Example phrasings | What happens |
|---|---|---|
| open | "open the terminal", "bring up the console", "give me a shell", "terminal please" | `app.open terminal`. If minimised, this restores it |
| open other apps | "show me how you're feeling", "show me your temperature", "show me the clock", "show my timers" | `app.open monitor` or `app.open clock` |
| minimise | "minimise / minimize / hide / put away the terminal", "show me your face" | `app.minimise` |
| restore | "show / bring back / switch to the terminal", "show it again" | `app.restore` |
| close | "close / quit / exit / shut / get rid of the terminal" | `app.close`, with a confirmation if busy |
| task | "write a script that…", "in the terminal, …", "do the following in the terminal: …", "make a program that…" | `term.task`, routed in code: recipe, then model script or model command. If the terminal is closed, it opens first |
| force the model | "… write it yourself", "… from scratch" | `force_model` |
| pause | "stop writing that script for a second", "pause", "hold on", "hang on", "wait", "stop" | `term.control pause`. While a program runs: freeze it |
| resume | "carry on", "continue", "keep going", "go on", "resume" | `term.control resume` (thaw if frozen) |
| cancel | "cancel that", "scrap it", "throw it away", "never mind the script", "forget that script", "stop it completely" | `term.control cancel`. Can be undone for 30 s |
| run | "run it", "run that again", "try it", "run snake" | `term.control run`, or `run_app` for a shelf program |
| fix | "fix it", "try again" | One model fix with the last 3 error lines |
| install | "install snake", "install an application", "what can you install" | Shelf, or the honest refusal (section 6) |
| report | "what happened", "did it work", "what did it say", "what are you doing", "are you done" | A sentence built in code from `term.status`. Never from the model |
| answer | "yes" / "no", within 15 s of a question | Close confirmation |

Rules for context:

- **Privacy commands come first.** "start guest mode", "end guest mode" and
  "forget that" keep their memory meaning. The one exception: "forget
  that/the script/program/code" means cancel while a job exists.
- **Commands that need a terminal or a job fall through when there is none.**
  close, minimise and restore without a terminal give "The terminal isn't
  open." pause, resume, cancel and run without a job give "I'm not writing
  anything." only when the terminal is open. Otherwise they fall through to
  conversation, so "continue the story" still works.
- **A bare "stop" pauses** and is reversible. Only "cancel", "scrap", "stop it
  completely" or Esc end things.
- **One job at a time:** "I'm already writing one. Say cancel first."
- **Apps switched off:** "My apps are switched off. You can turn them on in
  Setup."

Spoken lines are fixed in code, a few words each, and not archived:

| When | Pip says |
|---|---|
| Open | "Here's my terminal." Or: "It's already open." Or: "My terminal didn't open." |
| Minimise / restore | "Okay." Or, while writing: "Okay. I'm still writing." Restore: "Here it is." |
| Close | "Closed." While busy: "Something is still running. Close it anyway?" |
| A task | Recipe: "I know this one." Model: "I'll try." Install: see section 6 |
| Pause / resume / cancel | "Paused." The first time: "Paused. Say carry on when you're ready." Then "Carrying on." and "Cancelled." |
| Ready | "It's ready. Say run it." |
| Run | "Running it." Then "It ran without errors." or "It failed with exit code 2." |
| Report | Examples: "I'm writing it. Fourteen lines so far.", "It's paused at line 14.", "It printed 12 lines. They're on the screen." A line is read aloud only if it is 80 characters or less and mostly words |
| Stall or error | "I got stuck, so I paused." Or: "I lost my thought. Say carry on to try again." |

## 5. The safety cage and resource limits

### 5.1 Principles

- **The kernel cage is the boundary.** The command checks are there for honesty
  and to catch the small model's mistakes. They are not a security boundary.
  Assume anything running in the cage is hostile.
- **Voice acts only inside the cage.** Anyone in the room can talk to Pip, and
  the voice always reports the owner as the speaker. Anything that crosses the
  cage needs the keyboard: a network window, raised limits, keeping files.
- **Nothing runs as root on that screen.** Every organ runs as root today, and
  being-console forkpty()s its child as root. So the terminal never uses the
  Try menu's path.

### 5.2 Account and workspace

- **User `being-shell`** (in `BEING_USERS`):
  - locked password, home /workshop, shell /bin/sh;
  - no groups such as audio, video or input;
  - no login path on the laptop image.
- **User `being-panel`** for the renderers.
- **`/workshop`:**
  - a tmpfs, `size=64M,nr_inodes=4096,mode=0700`, owned by being-shell, mounted
    `nosuid,nodev,noexec`;
  - the mount point is made in post-build, because root is read-only;
  - RAM only, so it is gone at restart.
- **Scripts run through an interpreter**, `python3 x.py` or `sh x.sh`, never
  `./x`. noexec also stops native binaries and compiled extensions.
- **Shell start file** `/usr/lib/being/workshop/shellrc`, loaded through
  `ENV=`. It sets:
  - `PS1` with the hidden status marker, then `pip:~$`;
  - `PS2='> '` and `umask 077`;
  - `PATH=/usr/lib/being/workshop/bin:/usr/bin:/bin`;
  - `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, `LANG=C.UTF-8`,
    `TERM=dumb`.

### 5.3 The cage: one transient service per session

hands creates the pty pair and keeps the master. It starts the shell with a
fixed `systemd-run` command. Your words and the model's text reach the shell
only as pty bytes, never as arguments.

```
systemd-run --unit=being-shell-<n> --slice=workshop.slice --service-type=exec --collect --quiet \
  --uid=being-shell --gid=being-shell \
  -p TTYPath=/dev/pts/<N> -p StandardInput=tty -p StandardOutput=tty -p StandardError=tty \
  -p TTYReset=yes -p TTYVHangup=yes -p WorkingDirectory=/workshop \
  -p Environment="HOME=/workshop TERM=dumb ENV=/usr/lib/being/workshop/shellrc" \
  [the settings below] /bin/sh -i
```

| Setting | Works with today's image? |
|---|---|
| NoNewPrivileges=yes, empty CapabilityBoundingSet and AmbientCapabilities | Yes |
| ProtectSystem=strict, ProtectHome=yes, ReadWritePaths=/workshop | Yes |
| TemporaryFileSystem=/run (1M) and /var (1M): hides the bus socket, /run/being-*, D-Bus, systemd and the logs | Yes |
| TemporaryFileSystem=/tmp (16M) and /dev/shm (8M), instead of PrivateTmp, which is backed by the uncapped host /tmp | Yes |
| InaccessiblePaths=/mind /models /bootstate /etc/being | Yes |
| PrivateDevices=yes, PrivateIPC=yes, RemoveIPC=yes, KeyringMode=private, UMask=0077 | Yes |
| PrivateNetwork=yes: its own loopback only, so llama-server on 127.0.0.1:8080 is unreachable | Yes |
| ProtectKernelTunables/Modules/Logs, ProtectControlGroups, ProtectClock, ProtectHostname, ProtectProc=invisible, ProcSubset=pid | Yes |
| SystemCallFilter=@system-service, RestrictAddressFamilies=AF_UNIX, RestrictNamespaces, MemoryDenyWriteExecute, LockPersonality, RestrictRealtime, RestrictSUIDSGID | **No.** The defconfigs select no libseccomp, so systemd skips them silently. That includes being-eyes' current RestrictAddressFamilies |
| Belt and braces: nft `meta skuid "being-shell" drop` first in the output chain | Yes |

Facts from the code that this depends on:

- The bus socket checks no peer credentials; only its file mode protects it.
- llama-server has no authentication.
- RAUC's D-Bus policy accepts any caller.
- The cage settings that hide /run and the network are therefore load-bearing.
  The escape suite (task 25) must prove them.

### 5.4 CPU, memory and priorities

| Limit | Setting | Works today? |
|---|---|---|
| CPU share | workshop.slice, a direct child of `-.slice`, with `CPUWeight=idle`, plus SCHED_IDLE and idle IO class for the shell | Yes |
| CPU cap | `CPUQuota=50%`, half of one of the 4 logical CPUs | Yes (CFS bandwidth is on) |
| Memory | `MemoryHigh=192M`, `MemoryMax=256M`, `MemorySwapMax=0` | **No:** the kernel has no memory controller yet |
| Processes | `TasksMax=48` | **No:** the kernel has no pids controller yet |
| Backstops that work today | `LimitNPROC=48`, `OOMScoreAdjust=1000`, `LimitCPU=900`, `LimitFSIZE=16M`, `LimitNOFILE=128`, `LimitCORE=0`, tmpfs sizes | Yes |
| Time | 60 s per command Pip starts; `RuntimeMaxSec=4h` per session; close after 20 min minimised and idle | Yes |
| Output flood | hands relays at most 256 KB/s and stops reading the pty beyond that, so a flooding program blocks instead of burning CPU. The renderer draws at most 30 fps | Yes |

On the memory and pids controllers: a recent build log compiles kernel/cgroup
but neither mm/memcontrol.o nor kernel/cgroup/pids.o. The safety study read the
same from Buildroot's systemd fixups. Task 2 confirms it on the image, and task
5 turns both on for the next image.

Budget on 2 cores with hyper-threading (estimates):

| Load | Logical CPUs |
|---|---|
| Model writing code (2 threads, Nice 5) | about 2.0 |
| Renderer while text streams, dirty rows only, at most 30 fps | about 0.1-0.3 |
| Face, GPU-drawn; less fill in strip mode | small |
| Voice wake spotting while Pip types | about 0.1-0.25 |
| hands (typist, relay) | under 0.05 |
| The cage | at most 0.5, and about 0 while the model writes |

The real cost is hyper-threading. A script on a sibling thread slows token
generation, perhaps by 10-30 % (a guess). The quota bounds this, and M9
measures it. Nothing changes the organs' existing nice ordering. The cpu
controller only makes system.slice and workshop.slice peers.

Memory (estimates):

| Item | Memory |
|---|---|
| hands | about 25 MB |
| Each renderer | about 8 MB, plus a 3.3 MB frame file in /run |
| The second model slot | about 100-200 MB |
| The workshop | at most 64 MB |

All of this fits in 16 GB with a wide margin.

### 5.5 What Pip types, and what it refuses

- **Typing sanitiser.** Model text loses every control byte except newline. Tabs
  become four spaces, so ash's line editing never sees a completion key. ESC,
  NUL, 0x03, 0x04, 0x1a and 0x7f are dropped. Limits: 4 KB per request and
  16 KB queued.
- **Files are written as a quoted heredoc**, `cat > name.py <<'PIP_END_<random>'`,
  so nothing expands. A body line equal to the end word is refused. Names must
  match `[a-z][a-z0-9_-]{0,23}\.(py|sh|txt)` and never overwrite. Pip adds -2,
  -3 instead.
- **Cancel during a heredoc** sends Ctrl-C. ash drops the unfinished command,
  so no half file is written.
- **Text is typed only at the prompt**, when the shell is in the foreground.
  Pip never types into a running program.
- **One-line commands come from a grammar** (section 7.3) with an applet
  allow-list. It has no `;`, `&&`, `$( )`, backticks, variables or `rm`, and
  redirects only into a new plain name in the current folder.
- **Python scripts pass an `ast` check before they run:**
  - imports must come from an allow-list;
  - eval, exec, subprocess, os.system and input() are refused;
  - a missing import of an allowed module is fixed for free, by typing a
    visible `sed -i '1i import time' x.py`.
- **Your own typing** is caged but not screened (open question 6).

### 5.6 Switching it on

- `being-hands.service` has `ConditionPathExists=|/mind/apps.enabled` and
  `ConditionKernelCommandLine=|being.try=1`. The preset's `enable being-*`
  glob would otherwise turn it on everywhere.
- It also has `ProtectSystem=strict`, runtime directories `being-hands`
  (0700) and `being-panel` (owned by being-panel, 0750), `MemoryMax=128M`, and
  `Restart=on-failure`.
- A Setup toggle writes `/mind/apps.enabled`.
- Proposed default: on for Try USB images, off on an installed being until you
  switch it on (open question 1).

## 6. "Install an application"

**Recommended: the built-in shelf, plus an honest refusal.**

- The shelf is 5-8 tiny programs written for this console, under 200 KB in
  all, in `/usr/share/being/workshop/shelf/<app>/`. Each has an `app.json`
  with its name, spoken aliases, summary, size, sha256 and entry point.
- Candidates: snake (W/A/S/D), game of life, matrix rain, hangman,
  guess-the-number, stopwatch, dice.
- **"Install snake"** works like this:
  - Pip visibly types `install snake`. That consent comes from your request,
    and the helper only copies files into the workshop.
  - The helper prints that the program comes from Pip's built-in shelf and
    that no internet was used.
  - It copies the source to `/workshop/apps/snake/`.
  - A launcher in the read-only image runs the copy with python3, so noexec is
    not a problem.
  - "Run snake" starts it. "Uninstall snake" deletes it. Pip may edit the
    installed copy ("make the snake faster").
- **Apps must fit the console:**
  - TERM=dumb and 16 colours;
  - cursor moves and line and screen clearing;
  - printable keys, Enter and Backspace (arrows arrive with task 12).
- **For anything else, Pip says:** "I can't download programs. I'm offline and
  my system is read-only. But I have a shelf of little programs, or I can try
  to write you a small one."
- **Cost:** no network and no new attack surface. Installing is instant. The
  catalogue changes only with an image update.

The honest alternatives:

| Option | Cost | Verdict |
|---|---|---|
| Refusal only | Free and honest, but a weak showcase | Part of the recommendation |
| A USB shelf: you bring a stick with programs or pure-Python wheels. The existing read-only USB mount (nodev, nosuid, noexec) is used, and the import is confirmed on the keyboard | Moderate. No network | Later |
| PyPI through an approved window: Internet mode on, a keyboard confirmation, a separate `being-fetch` user in a one-shot unit, and an nft rule that allows TCP 443 for that user with a 10-minute kernel timeout. Only pure-Python wheels, installed into the workshop | Supply-chain and typosquat risk from a name chosen by voice. TLS and DNS paths. It weakens "offline by default". The Wi-Fi firmware is not packaged yet | Not in the proof of concept |
| A real package manager | The root is a read-only squashfs with A/B slots, and there is no package manager | No |

## 7. Code generation

### 7.1 Three tiers, tried in order

1. **Commands in code** (section 4): open, close, pause, run, install and the
   rest.
2. **Recipes.** About 12 tested scripts in
   `/usr/share/being/workshop/recipes/`, matched in code with typed slots (a
   whole number from 1 to 60, or a short word). Each has a lab test of its
   output.
   - countdown and count-up;
   - times table;
   - make a note, and list the notes in the workshop's `notes/` folder (never
     Pip's memories);
   - clock for N seconds;
   - dice and coin;
   - ASCII spiral, box, triangle, diamond, tree and heart;
   - "how warm are you" (thermal zones and battery from /sys. Offline, "show me
     the weather" says "I'm offline, so I can't see the weather. Here's how warm
     I am inside.");
   - disk, memory and uptime one-liners.

   Recipes are typed at about 30 characters a second under a
   `# recipe: … (from Pip's recipe book)` header, so they are never passed off
   as model-written.
3. **The model** (Qwen3.5-0.8B, thinking off) on slot 1, for everything else and
   for "write it yourself". The header reads
   `# written by my language model (Qwen3.5 0.8B)`.

Why recipes carry the demo:

- The only coding score found for Qwen3.5-0.8B is a third-party HumanEval run
  of 31.7 %, with an unclear setup.
- It will fail often on anything with geometry or an algorithm.
- Free-form dictation on the laptop mic will often mishear. Recipes use fixed
  phrases. The `# you asked:` line shows any mishearing before writing starts.

### 7.2 Prompt

- One byte-stable prefix of about 375 tokens.
- A short system rule: code only, one fenced block; Python 3 standard library,
  at most 25 lines, no input(), no internet, current folder; or one BusyBox
  line.
- Six example turns that are neighbours of the showcase tasks, not copies.
  One teaches the `import time` line, the most common small-model mistake.
- Each request adds about 25-35 tokens: `script: count down from 10` or
  `command: show the files here`. Code appends the opening fence.
- The request goes to raw `/completion` with token IDs and `id_slot: 1`,
  `cache_prompt`, `stream`, `return_tokens`, `n_predict: 400`.
- Sampling: temperature 0.2, top_k 20, top_p 0.9, `repeat_penalty 1.0`, and a
  seed taken from the request. There is no presence penalty: code repeats
  tokens.
- **Warming:** slot 1 is warmed once per boot, if apps are on. hands waits for
  60 s of unbroken idle, so it never competes with the mind's own warm-up of
  slot 0. If the terminal opens first, slot 1 is warmed then. Warming runs in
  32-token batches and stops at once on `hear.wake`. A cold read costs about
  13-25 s.

### 7.3 Grammars (GBNF)

- **Python body:** 1-30 lines, then the closing fence. Code also cancels any
  line over 100 characters.
- **Shell line:** up to three applets joined by ` | `, with an optional
  `> name` or `>> name`. Applets: ls cat echo date cal df du free uptime uname
  head tail wc sort uniq grep find mkdir touch cp mv seq printf tr cut basename
  dirname sleep python3. Arguments are plain words or simple quoted strings.
  Post-checks in code:
  - no `-exec` or `-delete`, and no `..`;
  - `cp -n` and `mv -n` only;
  - no `>` onto an existing file;
  - `python3` only with a file in the workshop.

  The image has no bash, only BusyBox ash.
- **Resume after a mid-line cut:** the rest of the current line, then up to the
  remaining lines.

### 7.4 Checks and the run loop

1. Strip the fence. If `ast.parse` fails, trim trailing prose lines and try
   again.
2. Apply the policy and fix a missing import for free. Python 3.12 and later
   name the missing module; this needs checking on Buildroot's Python.
3. A loop detector cancels the job if the same non-trivial line comes three
   times in a row: "I got stuck in a loop."
4. "Run it": Pip types the run line, and output appears live in the terminal.
   The limits are 60 s and 200 lines or 16 KB of output.
5. On failure, Pip states the exit code and the last error line, then offers
   "Say fix it". A fix sends the last 3 error lines (at most 200 characters)
   back on the same slot with seed+1 and temperature 0.4, and Pip retypes the
   whole file. Once only.
6. Honest words: "It ran without errors", never "it works".

Output is not read back to the model, except those few lines for a fix. On
this CPU reading costs about as much as writing (a 2 KB traceback would take
30-40 s), and output is a prompt-injection path.

### 7.5 The typist

- Text arrives in bursts. A jitter buffer smooths it:
  - it starts showing 0.8 s after the first token and aims for 1 s of lead;
  - it draws at the smoothed arrival rate, between 8 and 80 characters a
    second;
  - indentation costs nothing, and a line end costs a short beat;
  - once the stream ends it flushes at 150 characters a second.
- At most 25 pty writes a second.
- The codegen study simulated this. At 8-15 tokens a second there is less
  than one visible stall per script, and the last character appears 0.1-0.3 s
  after the last token.
- While the prompt is read, the status bar shows "reading your request… 3 s"
  (the true elapsed time).
- While minimised, text goes straight in without animation.

### 7.6 Pause and resume in llama-server

- **Two slots.** `--parallel 2 --ctx-size 8192`, 4096 per slot as today. The
  conversation is pinned to `id_slot 0` and code to `id_slot 1`. A code job
  then never evicts the conversation's cached prompt. Without this, the next
  spoken turn would re-read 300-1,500 tokens, about 15-90 s.
  - Slot 1 is never saved to a file. It is erased on close, on forget and on
    any guest change.
  - The fallback, if two slots fail measurement M3: save slot 0 and restore it
    around each job, about 1-3 s each way.
- **Hold:** typing stops, the stream keeps running. Used on the wake phrase.
- **Park:** the stream is cut (the existing socket shutdown), so the server
  stops within about one token. The token IDs are kept.
- **Resume:** the same request with `prompt = prompt IDs + generated IDs`.
  - If the server only has to extend its cache, this takes under 0.5 s.
  - Qwen3.5-0.8B is a hybrid recurrent model. If the server must roll back its
    recurrent state and has no usable checkpoint, it re-reads the whole prompt
    plus the partial script: about 400-700 tokens, 15-45 s.
  - That number (M4) is the main unknown. If it is over about 3 s at 200
    tokens, an explicit pause becomes a hold: typing stops and the remaining
    generation (at most 400 tokens) finishes quietly into the buffer. The
    status bar then says so honestly: "Paused · I'm still thinking it through,
    but I won't type until you say carry on".
  - Parking for a conversation turn still happens, because the conversation
    always comes first.
  - A plain-attention model (Qwen2.5-Coder-0.5B, or MiniCPM5-1B if it wins
    the separate conversation audition) avoids the problem.

### 7.7 Expected timings (estimates; slot 1 warm; Qwen3.5-0.8B)

Fixed costs:

- End of speech to final transcript: 0.5-1 s.
- Reading 30 new tokens: 1-2.3 s.
- Writing: 8-15 tokens a second, roughly 30-50 characters a second.

| Step | Recipe or code | Model |
|---|---|---|
| "Open the terminal" to panel shown | 1.5-3 s (shell unit, renderer start, 0.45 s rise) | |
| Countdown from ten on screen | about 4 s, then a 10 s run | 3.5-8.5 s, then a 10 s run |
| "List my notes" | 2-3 s | 6-11 s |
| ASCII spiral | 8-12 s | 8-25 s, and probably not a spiral |
| One-line command ("disk space") | 1-2 s plus the 4 s countdown | 1.5-3.5 s before the "run it" prompt |
| "Hey Pip" to typing stopped | about 0.5 s after "Pip" | same |
| "Carry on" | instant | under 0.5 s if the cache extends; 15-45 s in the worst case (M4) |
| "Run it" to first output | under 1 s | same |
| One model fix | | roughly doubles the job time |
| Cold slot 1 (not warmed) | | adds 13-25 s |
| Minimise / restore | 0.45 s | |
| Close | up to 3 s | |

### 7.8 A coder model

Qwen2.5-Coder-0.5B-Instruct (Apache-2.0):

- Vendor scores HumanEval 61.6 and MBPP 52.4. It should decode about 1.5x
  faster, and its plain attention cache makes pause, resume and fix reliable.
- RAM: about 0.6-0.8 GB. It would load when the terminal opens and stop when
  it closes. Loading takes 1-7 s, depending on whether the disk is an SSD.
- The real cost is a second signed model pack and wider forgetting coverage.

Adopt it only if the audition (task 4) shows at least 15 points better on the
showcase set and the laptop benchmark is not slower (task 27).

## 8. Forgetting, guests and honesty

- **Workshop files are memories**, so they get the same care:
  - In the proof of concept they live in RAM only and are gone at restart.
  - They are never in backups, because `backup.py` snapshots only settings and
    mind.db. Pip says so if asked.
- **Conversation memory** gets only a one-line episode, such as "I wrote
  spiral.py in the terminal", with the name and sha256 as metadata. Never the
  code or the output. Nothing at all is recorded in guest mode.
- **"Forget that script"** (with the confirmation that "forget" already uses)
  removes every copy:
  - the file and its same-hash copies and hard links in /workshop. They are
    overwritten with zeros, then unlinked through a directory handle with
    O_NOFOLLOW, so root never follows a link the cage planted;
  - the renderer's scrollback (a real wipe) and hands' 64 KB transcript ring;
  - slot 1 (`/slots/1?action=erase`);
  - the job state and the episode.

  Pip names any changed copies it finds and asks before removing them.
- **"Clear the workshop"** stops the session, remounts the tmpfs, and wipes the
  scrollback, the transcript, slot 1 and the job state. Entering or leaving
  guest mode does the same (`app.privacy`).
- **Logs** record only job ids, phases, exit codes and durations. Never typed
  text, code or output. The journal and the USB log export follow that rule.
- **Later, "keep that script"** would store it as a row in mind.db. That row is
  covered by the deletion ledger, backups and expiry. Never as loose files on
  /mind.
- **Honesty:**
  - recipes are labelled;
  - Pip never says a script works, only that it ran without errors;
  - it never says it can install software or see the weather while offline;
  - it never claims a panel opened before `app.state` says so.

## 9. Tasks

Effort: S is under a day of work, M is one or two days, L is three to
five days. Group A is probes and preparation. Group B is building blocks that
run in parallel with each other and are tested on the Mac. Milestones:

- **Milestone 1 (first demo):** the beats in section 1, with recipes and the
  model.
- **Milestone 2:** one breath, the shelf, the other apps and the focus gaze.
- **Milestone 3:** optional extras.

| # | Task | Files | Effort | Depends on | Parallel | Milestone |
|---|---|---|---|---|---|---|
| 1 | Owner decisions; amend the "no shell on the screen" and "can't write code" lines; document the `app.*`/`term.*` topics and the panel frame format | DESIGN-AND-PLAN.md, docs/PROTOCOL.md, docs/PANEL.md (new) | S | Owner answers 1-3 | A | 1 |
| 2 | Image assumption probe in QEMU, after the current build: seccomp flag in `systemctl --version`, cgroup controllers, freezer, busybox setuid, ash `$?` in PS1 and line editing under TERM=dumb, Tab inside a heredoc, TTYPath with PrivateDevices, TemporaryFileSystem layering, cpu.idle at the root | lab/tests/test_workshop_assumptions.py (new) | S | The image build now running | A | 1 |
| 3 | Cortex probe: `--parallel 2` RSS, id_slot honoured on both endpoints, slot isolation, unified KV default, resume cost after 50/200/400 tokens, cancel latency, grammar overhead | lab/task_resume_probe.py (new), docs/LAB.md | M | None (Mac Studio, then lab VM) | A | 1 |
| 4 | Code audition: 40 showcase prompts × 3 seeds with the real prefix and grammars; pass rate, tokens and estimated laptop time | lab/code_audition.py, lab/code_audition_prompts.json, lab/tests/test_code_audition.py (new) | M | None (Mac Studio) | A | 1 |
| 5 | Next-image prerequisites: CONFIG_MEMCG, CONFIG_CGROUP_PIDS, CONFIG_PSI; BR2_PACKAGE_LIBSECCOMP; sysctl `user.max_user_namespaces=0`; busybox not setuid plus a setuid audit; the /workshop mount point | os/board/beingos/linux.config, os/configs/beingos_laptop_defconfig, os/configs/beingos_x86_64_defconfig, os/package/being/50-being-workshop-sysctl.conf (new), os/board/beingos/post-build.sh | M | Task 2; owner answer 12; lands with the next build | A | 1 |
| 6 | Command parser with context rules, precedence (privacy first, the forget-script exception, pending close confirmation), `_COMMAND_START` additions, normalisation, tests with the owner's exact phrases and near-misses | mind/app_commands.py (new), mind/commands.py, tests/test_app_commands.py (new) | M | None | B | 1 |
| 7 | Workshop checks and grammars: GBNF builders (Python, shell, resume), shell post-checks, AST policy, sanitiser, heredoc writer, name slugs, free import fix, error tail | mind/workshop_check.py, mind/workshop_grammar.py, tests/test_workshop_check.py (new) | M | None | B | 1 |
| 8 | Recipe catalogue and matcher with slot checks and output tests | os/package/being/workshop/recipes/*.py, mind/workshop_recipes.py, tests/test_workshop_recipes.py (new) | M | Owner answer 5 (which sentences) | B | 1 |
| 9 | Typist and typing engine: jitter buffer, hold, pause, yield to a human, cancel, limits | mind/workshop_typist.py, tests/test_workshop_typist.py (new) | S | None | B | 1 |
| 10 | Cortex client: id_slot on every request (slot 0 for the mind), raw token-ID `/completion` streaming with return_tokens, grammar and seed, cancel, slot 1 erase | mind/cortex.py, tests/test_cortex.py | M | None | B | 1 |
| 11 | being-console panel mode: `--panel/--size/--io`, headless rendering, chrome with status bar, seqlock frame writer with dirty rows, OSC 7770 flags, real scrollback wipe, SIGUSR1/2 show and hide, poll loop, 30 fps cap; runs as being-panel; KMS path unchanged | os/package/being/being-console.c, os/package/being/being-console-draw.h, lab/console_preview.py | L | Task 1 (frame format) | B | 1 |
| 12 | Face compositor: frame discovery, header reads, texture upload, strip layout and transition, cover rules, key translation and forwarding to hands, F3, the M-key change | crates/face/src/panel.rs (new), crates/face/src/render.rs, crates/face/src/main.rs | L | Task 1; coordinate with the face fallback work | B | 1 |
| 13 | Face focus gaze from panel flags: typing, watching, paused; priorities against listening, speaking and presence; blink factor; no idle performances while Pip types minimised | crates/face/src/anim.rs | M | Task 12 | after 12 | 2 |
| 14 | hands organ: bus organ `hands`, app state machine, renderer lifecycle (hardened transient units), key socket, handling of state.changed, body.lid, key.interrupt, hear.wake and organ.up/down, re-announce on mind up | mind/hands.py (new), tests/test_hands.py (new) | M | Task 1; task 11 for integration | after 1 | 1 |
| 15 | Terminal session: pty, cage launch with the fixed property list, relay with 256 KB/s backpressure, OSC filter, prompt marker gated on tcgetpgrp, 60 s limit, freeze and thaw, kill all but the shell, RAM transcript, wipes | mind/terminal_session.py (new), tests/test_terminal_session.py (new) | L | Tasks 2, 14 | after 14 | 1 |
| 16 | Workshop job controller: recipe, model script and model command paths; slot 1 warm; hold, park and resume; cancel and undo; checks, free fix and "fix it"; run and countdown; `term.status`; stall watchdog | mind/workshop_job.py (new), tests/test_workshop_job.py (new) | L | Tasks 7-10, 15 | after 15 | 1 |
| 17 | Image wiring: users being-shell and being-panel, workshop.mount, workshop.slice, shellrc, being-hands.service with its OR conditions, nft skuid drop, Setup toggle and Try default, install of hands, recipes and launchers | os/package/being/being.mk, os/package/being/workshop.mount, workshop.slice, being-hands.service, workshop/shellrc (new), mind/network_enforce.py, mind/setup.py, mind/local_menu.py | M | Task 2 (task 5 for the full limits) | after 2 | 1 |
| 18 | Mind wiring: fixed replies, close confirmation, cortex lease (park before model turns; skip read-ahead, compaction and warm-up during a job), the "It's ready" announcement through mind.announce, code-built reports, guest and forget cascade, the one-line episode and the note to the model, tracking hands' presence | mind/turn.py, mind/commands.py, tests/test_turn_apps.py (new) | M | Tasks 6, 14 | after 6 and 14 | 1 |
| 19 | Promote `--parallel 2` and slot pinning into the cortex service, or adopt the save and restore fallback | os/package/cortex/being-cortex.service | S | Task 3 | after 3 | 1 |
| 20 | The shelf: 5 programs, `install`, `uninstall` and `shelf` helpers, image launchers, Pip's replies | os/package/being/workshop/shelf/<app>/, os/package/being/workshop/bin/*, os/package/being/being.mk, mind/app_commands.py | M | Task 17; owner answer 3. The programs can be written in group B | B (programs) | 2 |
| 21 | Body monitor and clock apps, each under its own renderer | mind/apps/__init__.py, mind/apps/monitor.py, mind/apps/clock.py (new); reuse mind/hardware_metrics.py and mind/reminders.py (read only) | S | Task 11 | B | 2 |
| 22 | Unit and integration tests on the Mac across hands, session, job and mind; Rust tests for header parsing, seqlock, layout and key translation | tests/*, crates/face/src/panel.rs tests | M | Tasks 12, 14-18 | after those | 1 |
| 23 | One breath "Hey Pip <command>": segment buffer, decoding the rest, command-only acceptance in the mind, replay suite rerun | mind/recognition.py, mind/voice.py, mind/turn.py, lab wake replay fixtures | M | Task 6 | B | 2 |
| 24 | Optional control grammar: JSGF with a garbage loop, grammar first while a terminal is open, re-decode by the language model off the event loop, dictionary check at build time | mind/recognition.py, os/package/being/control.gram (new), os/package/being/being.mk, tests/test_control_grammar.py (new) | M | Task 23; M6 shows it is needed | after 23 | 3 |
| 25 | Lab VM suite: open, close, minimise and restore over the bus; echo and finished status; close kills the cgroup; the cage-escape suite as being-shell; CPU cap with `yes`; F2 Try menu with a panel open; test_local_screen still 19/19; crop the lab eye detector to the strip while a panel is shown | lab/tests/test_apps.py (new), lab/tests/test_workshop_cage.py (new), lab/vm.py | L | Tasks 11-18 | after those | 1 |
| 26 | Measurement session on the real MacBook (section 10) | runbook in docs/LAB.md, results under out/voice-apps-bench/ | M | Task 25; needs the laptop and the owner | after 25 | 1 |
| 27 | Optional coder model pack: being-coder.service on 127.0.0.1:8081, started on open and stopped on close, its own signed pack and forgetting coverage | os/package/cortex/being-coder.service, mind/model_pack.py, os/package/being/50-being.preset | M | Task 4 shows +15 points; task 26 bench; open question 9 | after 4 and 26 | 3 |

Twenty-seven tasks:

- milestone 1: 21 tasks;
- milestone 2: 4 tasks;
- milestone 3: 2 optional tasks.

Fourteen tasks can run in parallel:

- the probes, tasks 2-4, can start today;
- task 5 can be prepared now but merges only once question 12 is settled;
- tasks 6-12, 20 (the programs), 21 and 23 start once task 1 has fixed the
  topics and the frame format.

The critical path is 1 → 14 → 15 → 16 → 18 → 25 → 26.

## 10. What must be measured on the real laptop

| # | Measure | Decides |
|---|---|---|
| M1 | Face frame cadence and CPU with a panel shown: idle, while Pip types, during an output flood | Whether the compositor and the 30 fps cap are enough |
| M2 | Renderer cost per typed character and during a flood | The dirty-row and 30 fps design |
| M3 | Cortex tokens per second (reading and writing) at 2 threads with the voice running; with the face or a panel shown; `--parallel 2` RSS; slot 0 first-word time before and after a code job | Two slots (task 19) |
| M4 | Resume cost after a pause at 50, 200 and 400 tokens, with 1 and 2 slots (`prompt_n` in the timings) | Whether an explicit pause parks or holds |
| M5 | Cancel latency, from cutting the stream to the CPU being free (ubatch 128) | Pause responsiveness |
| M6 | Wake hit rate and false wakes, idle versus during generation with the fans up (50 trials each); command accuracy on about 40 of the owner's own recordings; one-breath false-command rate on the 172-case wake replay | Whether task 24 is needed |
| M7 | Latencies: end of speech to final; "Pip" to typing stopped; "open the terminal" to panel shown; "run it" to first output | The demo timings in section 1 |
| M8 | Microphone overruns (arecord) during 5 minutes of generation | Voice priority against the cortex |
| M9 | The cage: `systemd-analyze security` on the session unit; the cgroup files (cpu.idle, cpu.max, and memory.max and pids.max after task 5); a `yes` loop at quota and its effect on tokens per second | The CPU cap and the hyper-threading cost |
| M10 | Package temperature, watts and fan noise in four arms: idle, generation, a script at quota, and generation plus script plus flood | The heat and battery thresholds |
| M11 | Disk type and model and slot load times | The coder-model cost |
| M12 | Key forwarding latency and the US-layout mapping on the built-in keyboard | Task 12 |

## 11. Open questions

1. May DESIGN-AND-PLAN.md's "no shell on the screen" and "it can't write code"
   become "no shell unless the owner switches on showcase apps"? Default on for
   Try USB images and off on an installed being until Setup turns it on?
2. Running: a script runs only when you say "run it", and a one-line command
   Pip wrote runs after a 4-second countdown you can stop. Or should Pip always
   ask "Shall I run it?"?
3. "Install": is a built-in shelf plus "I can't download, but I can write you
   one" right for the proof of concept? Which five programs (snake, life,
   matrix rain, hangman, guess-the-number, stopwatch, dice)? Can network
   installs wait until after the proof of concept?
4. Is a RAM-only workshop right for now: everything gone at restart and never
   in backups? A later "keep that script" would save it into memory, where it
   is backed up and can be forgotten.
5. Are pre-written recipes for the common requests acceptable, labelled "from
   my recipe book", with "write it yourself" forcing the model? Which 5-10
   sentences should be rehearsed for the demo?
6. May the owner type into the terminal directly (caged, not screened)? Or should it
   be voice-only, with the keyboard limited to Esc, F3 and PgUp/PgDn?
7. May guests use the terminal, with a fresh workshop wiped when guest mode
   ends? Or the owner only?
8. When the lid closes or Pip sleeps, should running programs freeze (proposed)
   or keep running until their time limit?
9. Is a second model slot acceptable? It needs more RAM and touches the
   conversation cache path, so it is measured first. And, if the audition
   shows a clear gain, a second small coder model loaded only while the
   terminal is open?
10. Are the body monitor and the clock the right other two apps, or should
    one be swapped for a notes pad? Eye strip of 160 px (smaller eyes, 23 terminal
    rows) or 200 px (21 rows)?
11. Is it acceptable that one-breath "Hey Pip <command>" works only for
    commands, while conversation still needs "Hey Pip", a pause, then speech?
    Short spoken acknowledgements, or silence with the screen change as the
    answer?
12. May the next image turn on the memory and process-count controllers (and
    PSI), add libseccomp, set `user.max_user_namespaces=0` and remove
    BusyBox's setuid bit? This also makes being-eyes' existing sandbox settings
    take effect.
13. Can about 40 short command utterances be recorded on the MacBook's own mic,
    in a quiet room and again with the fans running?

## 12. Main risks

- **Recognition** on a far-field laptop mic with fans under load may stay poor
  even for commands. Esc, the on-screen `# you asked:` line and "run it" are
  the safety nets. M6 decides whether the showcase is credible.
- **Resume on the hybrid model may be slow** (M4). Hold is the fallback, at a
  CPU cost.
- **Several cage limits do nothing on today's image:** memory, process count
  and every seccomp setting. Until task 5 lands, the limits that actually hold
  are the tmpfs sizes, LimitNPROC, OOMScoreAdjust, CPUQuota and idle
  scheduling.
- **The cage hides services that trust any local caller:** the bus socket
  (protected only by its file mode), llama-server and RAUC. Their protection
  depends on the TemporaryFileSystem and PrivateNetwork settings being right.
  Task 25 must prove them.
- **Face and console edits overlap with other work in progress** on the face
  and console fallbacks. The compositor stays in panel.rs behind a small hook in
  main.rs.
- **BusyBox ash details are unverified:** `$?` in PS1, line editing under
  TERM=dumb, and Tab in a heredoc. If the marker fails, the exit status is
  reported as unknown.
- **The lab eye detector will mistake terminal cyan for eyes** unless it crops
  to the strip. llvmpipe timings in QEMU do not transfer to the laptop.
- **The 0.8B model will often write wrong code.** The showcase leans on recipes
  and the shelf, and Pip says "I'll try" when the model writes.

## Decisions (23 November 2026)

The open questions in section 11 were settled as follows:

1. **Opt-in or on:** on by default on Try images and installed beings; Setup has a switch to turn it off. The original "no shell on the screen" rule now means "no login shell"; the caged workshop shell is allowed.
2. **Running code:** scripts and one-line commands Pip writes wait for "run it". A built-in one-line recipe runs after a 4-second countdown that "stop", Esc or any key cancels. Shell commands must pass the workshop policy. This follows Shell Lab decision S7 from 25 November.
3. **Install:** the offline shelf of five to eight tiny console programs plus the honest refusal line. Network installs wait until after the proof of concept.
4. **Workshop:** RAM-only, cleared at reboot, never backed up; "forget that script" deletes the file and its transcript.
5. **Recipes:** labelled recipes for the rehearsed demo sentences, and the model for everything else; Pip says which it used if asked.
6. **Keyboard:** the owner's keyboard may type into the terminal; Pip's typing pauses while a key is held.
7. **Guests:** no terminal in guest mode.
8. **Lid and sleep:** programs freeze when the lid closes or Pip sleeps, and resume on wake.
9. **Model:** a second llama-server slot for code; an optional coder model pack only after the real-laptop benchmark.
10. **Apps:** terminal, body monitor and clock; the eye strip is 160 px.
11. **One breath:** "Hey Pip, <command>" in one breath works for commands; Pip gives short spoken acknowledgements ("Opening it." / "Paused.").
12. **Kernel:** the next images add the memory and pids cgroup controllers, PSI, libseccomp and user.max_user_namespaces=0, and drop BusyBox's setuid bit. Not on the frozen first-boot USB.
13. **Recordings:** the ~40 command utterances are recorded on the real microphone after the first boot.

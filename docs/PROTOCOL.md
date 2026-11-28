# The bus

Every organ talks to the brainstem over one Unix-domain socket. Each message is
one JSON object on one line. The `t` field is the topic; everything else is data.

```
{"t":"hear.final","text":"what time is it","speaker":"owner"}
```

Audio and video never cross the bus. They stay inside the organ that owns the device.

## Socket path

1. `$BEING_BUS` if set
2. `$XDG_RUNTIME_DIR/being/bus.sock` if `XDG_RUNTIME_DIR` is set
3. `/tmp/being-$USER/bus.sock`

## Joining

The first line an organ sends is `hello`, with its name and the topics it wants:

```
{"t":"hello","name":"face","sub":["state.changed","face.*","say.level","stem.beat"]}
```

A subscription is an exact topic, a prefix ending in `.*`, or `*` for everything.
An organ never receives its own messages back. The brainstem replies to `hello`
with current readings, including `body.lid` before `state.changed`, so a
restarted organ receives its privacy state before processing conversation.
Voice and mind output can carry the lid sequence as `lid_seq`. The brainstem
drops packets from an earlier lid state.

Eyes starts with capture denied. It requests permission only when the owner
has enabled the camera. The brainstem grants an epoch for a confirmed open
lid in an awake state. Eyes stops capture on revocation, lid uncertainty,
or loss of the bus or heartbeat. The brainstem consumes detection metadata
itself and forwards only gaze targets to the face. Detections expire after
1.5 seconds. Camera metadata does not start listening.

The Lists, Memory, Journal, and Mind panels read one page at a time from the mind through
`organizer.sock` alongside the bus socket. Its filesystem permissions limit it
to the owner of the mind and hands processes (root on the appliance). The
reader refuses guest and session-only requests. Rows go to the panel renderer
and do not cross the shared bus. The panel and its runtime frame are cleared
on close or guest entry. The Mind panel uses the same channel to read
RAM-only counts by `mind.route` category and up to ten route IDs.

## Topics

| Topic | Sent by | Fields | Meaning |
|---|---|---|---|
| `hello` | any organ | `name`, `sub` | Join the bus |
| `organ.up`, `organ.down` | brainstem | `name` | An organ joined or left |
| `stem.beat` | brainstem | `state`, `up` | Heartbeat, twice a second |
| `stem.budget` | brainstem | while active: `active: true`, `profile`, nullable `pressure`, `priority`, `state`, `epoch`, `ttl_ms`, `observe_only: true`; on revocation: `active: false`, `reason`, `epoch` | A sanitised observe-only readout of current Bodycare resource advice. Brainstem accepts it only from the authenticated active Bodycare connection, only when its profile and echoed epoch match the current state, and for at most 12 seconds. `deep_sleep` and `dreaming` additionally require Brainstem's authoritative confirmed-closed laptop lid; the dream stage itself is Bodycare-attested from its authenticated `mind.dreaming` input. Snapshots carry remaining rather than original TTL. It is revoked on unavailable or rejected advice, replacement or loss of the producer, expiry, lid authority loss, or state-epoch change. It never changes a unit, scheduler, governor, cap, or device. |
| `state.changed` | brainstem | `state`, `state_epoch`, `prev`, `muted`, `unwell`, optional `speech_end_mono_ms` | The being's state or an overlay changed. `state_epoch` advances on every state transition, including a later return to the same named state, and is present in subscription snapshots. Speech timing belongs only to the live Thinking transition for an accepted utterance. |
| `body.state` | brainstem | `battery`, `battery_low`, `charging`, `on_ac`, `temp`, `lux`, `lid_closed`, `hour`, `minute`, `health_pct`, `cycles`, `reflex_strain_pct`, `package_w`, `effort`, `thermal_throttles`, `oom_organs` | Body readings. Unavailable readings are null. The ambient-light reading is approximate. Mind's body skills and morning brief expire this report after 15 seconds without an update. The CPU strain percentage and breathing effort (0–1) remain zero until the operating level holds 30 seconds. Package watts come from the energy counter. The thermal counter is since boot, and the OOM bits in order are face, voice, brainstem and mind. |
| `body.care` | bodycare | the care readings, plus `charge_limit`, `cap_honoured`, and observe-only resource advice | What bodycare is doing for the body. `charge_limit` is the charge cap it asked for (a percentage, or null). `cap_honoured` is true only when the battery has really stopped charging at that cap, so Pip never claims a cap that is not holding. `resource_profile`, `resource_pressure`, and `resource_priority` are descriptive WP-D8 evidence only; `resource_state_epoch` echoes the exact Brainstem state snapshot used. The priority records thermal before power source, lid, and cognitive state. `resource_observe_only` is true, `performance_eligible` is false, and consumers must discard the advice after `resource_ttl_ms`. Unknown sensors leave pressure null. A lost brainstem state makes the profile and epoch null and its TTL zero. Mind's body skills and morning brief expire this report when bodycare leaves the bus or after 30 seconds without an update. |
| `body.lid` | brainstem | `closed` (boolean or null), `seq`, `show_face`, optional `snapshot` or `dreamed` | Only confirmed open permits capture. A retained snapshot catches a new subscriber up. While the lid reading is unknown, `show_face` can restore the panel locally. `dreamed` marks a live opening after completed journal work during that closure. |
| `body.presence` | brainstem | `present`, `lid_seq`, `camera_seq` | A presence reading tied to the current open lid and camera grant. It carries no coordinates. Mind discards old sequences and expires detections after 1.5 seconds. Closing the lid, revoking the grant or losing Eyes clears the detection immediately. |
| `body.plug` | brainstem | `kind: storage`, `attached`, `place: left` or `nearby` | Linux noticed a USB storage device arrive or leave. The initial inventory and PipOS's own system, model, memory and boot-state disks are excluded. The observation sends no identifiers or labels and never mounts the device. The lit face can glance at an arrival. Mind may privately correlate an arrival with bounded, already-cached sysfs/udev metadata for an ephemeral owner announcement; that metadata is never added to the bus or retained. |
| `body.mic_policy` | brainstem | `breach: true`, `action: safety_latched`, optional `snapshot: true` | The mapped physical ALSA capture stayed open for more than one second after capture permission closed. Brainstem marks the being unwell, latches Ears and Voice off until reboot, and the Mind records the live event without owner words. New subscribers receive the latched state as a snapshot. The active boot-time microphone measurement and diagnostic audio sessions are exempt while their markers are present. |
| `panel.ready` | face | `seq` | The first frame for this lid reading has been presented. The brainstem can restore panel power. |
| `body.senses` | brainstem | `line` | One line of true facts about the body, for the mind to show the model |
| `body.drives` | brainstem | `energy`, `arousal`, `social`, `comfort`, `tiredness`, `eagerness` | Slow internal numbers. The shared face formulas set tiredness up to 0.26 and the other scales run from 0 to 1. The opt-in reply experiment discards samples older than 15 seconds. |
| `face.express` | brainstem | `name`, `hold`, optional `intensity`, optional `blend` | Show an expression for `hold` seconds (clamped 0.2-600). `intensity` is a number 0..1 (clamped) that scales the pose from neutral; a non-number or non-finite value is ignored and counts as 1.0. `blend` is an object `{name, mix}`: `name` must be a name `face.express` accepts (not `dreaming`, not unknown, not the main pose's own name) and `mix` a finite number clamped to 0..1; the main pose is mixed with `blend.name` by `mix`, then `intensity` scales the result from neutral. An invalid `blend` is dropped and the pose plays alone. Missing fields mean full strength and no blend, as before. Marks and kicks scale with `intensity`. Suppression while muted, dazed, asleep or dark is unchanged |
| `face.gaze` | brainstem | `x`, `y`, optional `hold`, or `present: false` | Track a point from -1 to 1, glance for `hold` seconds, or release the camera target |
| `hear.wake` | voice | | Its name was heard |
| `hear.speech_start` | voice | | Someone started talking |
| `hear.partial` | voice | `text` | Words so far; may be revised |
| `hear.final` | voice | `text`, `speaker`, optional `speech_end_mono_ms` | The finished utterance. Voice may attach an estimated last speech timestamp in Linux monotonic milliseconds. Brainstem forwards it only if it is no more than 3000 ms old and the utterance starts a new Thinking transition. |
| `hear.cancel` | voice | | False start, nothing was said |
| `ears.policy` | brainstem | `allowed`, `seq` | The isolated Ears organ may capture only while this current grant is true. A closed or unknown lid and mute revoke it. |
| `ears.capture` | ears | `overruns`, `active` | Count of ALSA capture overruns from the authenticated Ears service. The count starts at zero for each new capture and freezes when capture ends. Carries no audio or process output. |
| `hear.missed` | voice | `reason`: `no_speech` or `unclear` | It was listening and could make nothing out. The voice owns up itself ("Sorry, I didn't catch that.") once per wake, unless that wake was already answered with words (a cough in the follow-up window gets no apology); the mind only shows concern, and the face squints briefly. No words are carried. |
| `voice.level` | voice | `floor_dbfs`, `speech_peak_dbfs`, `gain`, `heard_ago_s`, `asr`, `tts` | How well it can hear: the room's noise floor and the last speech peak in dBFS, the software gain applied to the microphone, seconds since speech was last understood, and the recogniser and voice in use. Sent every 30 s while resting, and on request. Numbers and engine names only, never words. |
| `voice.level_request` | mind | | Ask the voice for a `voice.level` now ("How well can you hear me?") |
| `voice.volume` | mind | `level` (0 to 100) or `step`, optional `persist`, optional `feedback` | "Speak up", "be quieter", "set your volume to 5", or a volume or speaker-mute key. The voice owns the mixer and sets it. `persist: false` (a speaker mute from a key) sets the level for now only: the saved level in `voice.json` and the sound card's stored state stay as they were. `feedback: true` (the volume keys) plays one short click at the new level, never at 0, and only when a chirp could play and the microphone is not open for the owner's words |
| `voice.volume_state` | voice | `level`, `mixer` | What the voice really set, so the mind's answer is true. The face shows it as a level bar. After a key mute `level` is 0; the report the voice sends when the mind connects never is |
| `voice.install_status` | local installer screen | `stage`, `issued` (monotonic seconds), `lid_seq` | Best-effort local Flite status in Try after confirmation. The brainstem accepts only the local screen unit, an open current lid and fixed stages. Voice drops stale or muted status and keeps it outside the reply protocol. |
| `voice.prepare` | mind | `lid_seq`, `clauses` | Optional overnight synthesis of up to eight short clauses. Brainstem routes this only between authenticated Mind and Voice units, without journaling it, for the current confirmed closed lid while asleep and in the owner session. Voice rechecks permission, power and temperature. Preparation produces no playback. |
| `voice.clear_prepared` | mind | none | Cancel any overnight synthesis and clear prepared audio. Goes directly to the authenticated Voice unit. |
| `voice.chirps` | mind | `on` | Turn the chirps (earcons) on or off; the voice keeps the choice in `voice.json`. No spoken command sends it yet |
| `stem.chirp` | brainstem | `name` | Play one named chirp. The voice listens for it; the brainstem does not send it yet |
| `voice.engine` | mind | `tts`: `piper`, `flite` or `auto` | Choose the speaking voice. `auto` uses the natural voice (Piper, Alba) from the first reply and changes to the original voice (Flite) only if the natural voice fails to verify or load, or measures slower than 0.7 of real time on this laptop. The voice keeps the choice in `voice.json`. `being.tts=` on the kernel command line, or `BEING_TTS` in the voice's environment, overrides the choice for that boot. |
| `say.request` | mind | `text`, `seq`, `last`, optional `announcement`, `chirp`, `private`, `face_cue`, `confirm_ack`, `reply` | Speak this accepted clause. `face_cue` is a reply emotion for this clause only. An empty final marker ends the reply and carries the attempt token for a reminder, timer or greeting. A confirmation question may put one opaque 32-hex `confirm_ack` on that empty final marker only; Voice never speaks it. `private: true` sends the clause only to the local Voice and Face and omits it from the hub journal. `chirp: "bell"` requests the bell for an eligible ring. Every reply's clauses (a model reply's, and a fixed reply's such as a command's answer, a reminder or a stock line), the curiosity question that may follow a model reply, and the final marker carry `reply`, one opaque 16-hex token per reply, so Voice can report which clauses played (`say.played`). Only the camera's private description has none. |
| `say.started` | voice | | First audio is playing |
| `say.level` | voice | `v`, optional `face_cue` | Loudness of its own voice right now, 0 to 1. Voice sends a clause cue once, with its first audible playback sample. The face clears it when playback ends or is interrupted. |
| `say.done` | voice | `final`, optional `announcement`, `confirm_ack` | Queue is empty. `final` is true after the last clause. After successful announcement playback the voice echoes the attempt token. After a confirmation question completes without a synthesis or playback failure, authenticated Voice echoes its opaque `confirm_ack`; Mind requires the exact receipt plus its current bus and lid session before opening the yes/no window. Failed or unrelated replies cannot acknowledge it. |
| `say.played` | voice; the mind in written replies | `reply` with `seq` (and, from the mind, `text`), or `reply` with `final: true` and `stopped` | Playback receipts for a `say.request` that carried `reply`. With `seq`: that clause's first audio is playing, whichever way it was made (Piper, Flite, a stock clip or prepared night speech); a clause the speaker rejects is never reported. With `final`: every clause receipt of that reply has been sent, and `stopped` is true when Voice stopped before the reply's end (`say.stop`, a failure, or an end that never came). Voice ends each reply it accepted this way, before its `say.done`, except while the lid is closed or the bus is gone. Brainstem accepts it only from the current Voice connection in the current lid state, and from the current Mind connection only while the retained `chat.mode` has `reply: "text"`: then nothing is played (and the voice may be down), the mind sends no `say.request`, and each of its receipts carries the clause's words in `text`, which Face captions and the typing chat show; a `text` field on a voice receipt is ignored when a held clause matches. Mind keeps what played; Face captions follow it. See [Interrupted replies](#interrupted-replies). |
| `say.stop` | brainstem | optional `reason` | Stop speaking at once. `speak_timeout` indicates stalled playback. |
| `mind.ready` | mind | `cortex` | The mind is up; `cortex` says whether the model answers |
| `mind.emotion` | mind | `name`, `hold` | An immediate non-speech expression from Mind, such as curiosity or a missed-word reaction |
| `mind.reply_done` | mind | `spoke` | The reply is fully generated. `spoke` is false if nothing is to be said |
| `mind.announce_cancel` | mind | `announcement` | Revoke only the unsolicited announcement carrying this exact current token. Brainstem ignores stale or unrelated tokens; a match owns the speaking turn and makes Brainstem issue `say.stop` and `mind.cancel`. |
| `mind.route` | mind | `via` | Optional caption indicator. One of `code`, `model`, `waiting`, or `unavailable`. It contains no request or reply text and is absent from private and guest turns. |
| `mind.cancel` | brainstem | optional `reason` | Abandon the reply in progress. `think_timeout` indicates a reply that failed to start. |
| `mind.announce` | mind | `announcement`, `lid_seq`, optional `idle_only` | Request a chance to speak with the current lid reading. First-daily greetings require the idle state. |
| `mind.dream_committed` | mind | `lid_seq` | Report a committed overnight batch with journal entries for the current confirmed closure. The brainstem consumes the receipt locally and acknowledges it only on the next confirmed opening. |
| `mind.clock` | mind | `utc_offset` (seconds east of UTC, or null), `trusted` (bool) | The owner's time-zone offset and clock trust. Sent on each new bus connection and when the offset, trust or persisted floor changes. With a trusted clock and no zone, the brainstem keeps system time. While trust is false, the senses line reports uncertainty and time alone cannot trigger drowsiness. |
| `mind.dreaming` | mind | `active` | The dream work (the journal or the dream model) started or stopped. Bodycare holds the package at its dream target while it is true; night upkeep alone is not sent |
| `stem.shutdown` | brainstem | `reason` (`battery_critical` or `lid_closed_on_battery`), `in` (seconds), `percent`, `minutes_left`; or `reason` and `cancelled: true` | The battery calls for a power-off in `in` seconds: the mind saves and says goodbye. Plugging in, or a reading that turns out healthy, cancels it |
| `mind.shutdown_ready` | mind | `reason` | The mind has saved for this power-off; the brainstem may switch off before the grace ends |
| `mind.progress` | mind | `phase`, optional `tokens` | The cortex is still working on this reply (a slow cold read is not a hang). Being added by plan task 1.15; the fields settle with that work. |
| `life.woke` | brainstem | `id`, `born`, `wakes`, `woke_at`, `first`, `lost_power`, `new_body`, `body`, optional `slept` | The life record for this wake: its identity, birth time, wake count, whether this is the first wake, whether the last wake ended in a power loss, whether it woke in a different body, and how long it slept. |
| `stem.announce_ready` | brainstem | `announcement` | The voice is free and the brainstem has entered thinking for the matching announcement. |
| `being.sleep`, `being.wake` | mind | | Go to sleep or wake up, on request |
| `being.brightness` | mind | `step` (+1 or -1) or `level` (0 to 100), optional `by`, optional `req` | "Brighter", "dimmer", "set your brightness to 50 percent", or a brightness key. The brainstem writes the panel backlight (intel_backlight first), held to 5-100%, and restores it on a clean stop. A step moves 15 points, or `by` points (1 to 50; the keys send 6). Only the mind may send it. (`body.*` topics are reserved for the brainstem.) |
| `body.brightness` | brainstem | `ok`, `level`, `at_max`, `at_min`, `limited`, `reason`, optional `req` | The answer to `being.brightness`: the level it now has, whether it is at an end, or why nothing changed (`bad_request`, `no_control`, `unreadable`, `write_failed`). It carries the request's `req` back when that is a short tag (letters, digits, `_`, `-` or `.`, up to 32 characters). The keys tag theirs `key`, so a spoken "brighter" is never answered by a key pressed at the same moment. The face shows the level as a bar. |
| `being.kbd_light` | mind | exactly one of `step` (+1 or -1), `level` (0 to 100) or `toggle: true`; optional `req` | A keyboard-light key. The brainstem writes the applesmc LED `smc::kbd_backlight` (any LED whose name ends in `kbd_backlight` stands in). A step moves one sixteenth of full; a level is written as that percentage. A toggle turns a lit keyboard off, or an unlit one back on at its last level (50% if it was never lit). The light goes out while the panel is dark and comes back with it, and the level found at start is put back on a clean stop. Only the mind may send it. |
| `body.kbd_light` | brainstem | `ok`, `level`, `on`, `at_max`, `at_min`, `limited`, `reason`, optional `req` | The answer to `being.kbd_light`, with `req` carried back as for `body.brightness`: the level it now has, or why nothing changed (`bad_request`, `no_control`, `unreadable`, `write_failed`). The face shows the level as a bar. |
| `app.open`, `app.close`, `app.minimise`, `app.restore` | mind | `app`: `terminal`, `monitor`, `clock`, `calc`, `lists`, `memory`, `journal` or `mind`, `req`, optional `page` | Show, close, minimise or bring back one workshop panel. `app.open` can select `page: processes` on Monitor. At most one app is shown. Minimising a small app closes it. |
| `app.calc_display` | mind | `expression` and `result` as bounded printable text, `error`, `lid_seq` | Update the open calculator with a code-computed spoken sum or its error. Suppressed while the lid is closed, omitted from the bus journal, discarded if the app is closed or in guest mode. Calculator keystrokes use the existing private hands socket. |
| `app.lookup_query` | mind | `app: lookup`, `query`, `req`, `lid_seq` | Submit an explicit spoken lookup. Hands also opens `lookup` through the regular app-open route. Typed queries and results travel over the root-only local Organizer socket, with owner Internet mode and web lookup permission checked on each request. Suppressed while the lid is closed and omitted from the bus journal. |
| `screen.night` | mind | `stages`, `lid_seq` | The current wake may show the number of finished night-shift stages for eight seconds. It clears on lid or guest changes and is hidden behind an open panel, captions or level bar. The Journal's Last night page has the stage names. |
| `app.state` | hands | `app`, `view` (`shown`, `minimised` or `closed`), `session`, `job`, `req` | What the workshop now shows. A small app carries `session` and `job` as null. A small app whose renderer ends is announced closed without a request. |
| `term.status` | hands | `id`, `phase`, `source`, `file`, `lines`, `seconds`, `exit`, `reason`, `speakable`, `describe`, `kind`, optional `sha256`, `fixed`, `req` | The state of the one terminal job. `phase` is one of `reading`, `writing`, `held`, `paused`, `checking`, `waiting`, `countdown`, `running`, `frozen`, `done`, `failed` or `cancelled` (and `forgotten` when a finished job is dropped). `exit` is the process's exit code once it ran. `reason` explains a stop (for example `exit_unknown`, `heat`, `policy:*`). `speakable` marks a line the mind may announce; guests never hear workshop news. A status can be re-sent on a query, so receivers de-duplicate by `id` and `phase`. The mind builds its sentence from it in code. The brainstem also reacts to `failed` with a non-zero `exit` and to `done` with `exit` 0 by sending `face.express` (see Expressions). It is not lid-gated. |
| `app.refused` | hands | `req`, `reason`, `app`, optional `job` | The workshop would not do it (for example `busy`, `no_session`, `low_battery`, `too_warm`). `no_face`: the Try menu has the screen (`being-face.service` is not running and `being-local-screen.service` is), so a panel opened now could not be seen. The mind says "Close the Try menu to see my terminal." (or clock, or monitor), and a job that needed the terminal brought forward does not start. |
| `app.privacy` | mind | `guest` | A guest arrived (`true`) or left. The hub replays privacy to reconnecting organs. On the appliance `/run/being/guest` preserves guest mode across organ restarts within a boot. A restarted Mind starts with an empty guest conversation. Hands clears the workshop. Voice suppresses transcript logging, and Face clears and suppresses words and routing indicators in captions. |
| `screen.prefs` | mind | `glow`, `shape`, `strength`, `size`, `brows`, `mouth`, `revision`, `menu_position`, `dock`, `menu_labels`, `captions`, `caption_heard`, `caption_cost`, `idle_glances`, the three `*_widget` flags and their `*_widget_position` fields | Saved display settings. Menu position is left, center, right or off. `dock` is `fades` (the default: the dock at the top is always visible and shrinks to a pill when idle), `always` or `off`. Each widget accepts top-left, top-right, bottom-left or bottom-right. Captions default off. The brainstem retains the latest message for a new face connection. |
| `screen.prefs_state` | face | Same fields as `screen.prefs` | Validated appearance applied by the face, reported on a change and on reconnect. The revision prevents an older retained request from replacing a newer file. Both appearance topics carry settings only and can pass while the lid is closed. |
| `screen.quick_menu` | mind | Optional `open`, `position`, `options`, `options_help`, `options_status`, `lid_seq` | Show or dismiss the Quick Menu or Options. Options receives up to 48 bounded registry help lines and a fixed message if a setting cannot be saved. This event is ephemeral and blocked while the lid is closed. |
| `screen.online` | mind | `allowed`, `lookup`, optional `weather`, `lid_seq` | Short-lived desktop eligibility and latest requested current-weather reading. Face expires it after three seconds without a refresh and clears it on lid or guest transitions. Suppressed while the lid is closed and omitted from the bus journal. |
| `ui.action` | face | `id`, `via`, `lid_seq`, with `field` and `value` for `shell.options.set` | A registered keyboard selection, Care shortcut or options request. The Mind validates Options fields and values through the existing appearance store for the current open-lid owner session. The brainstem blocks the event while the lid is closed. The dock's mode chip and the F7 key send `chat.mode.next`, `chat.mode.spoken`, `chat.mode.typed_voice` or `chat.mode.typed_text`, which the chat bridge acts on. |
| `ui.text` | face | `text` (1 to 400 printable characters), `lid_seq` | A line the owner typed in the face's dock. Only the face may send it, and the brainstem blocks it while the lid is closed. The chat bridge passes it on as `hear.wake` if needed, `hear.speech_start` and `hear.final` (speaker: the owner) when a written mode is on and no guest is here; otherwise it is dropped. It is never logged or kept. |
| `chat.mode` | chat bridge | `mode` (`spoken`, `typed_voice` or `typed_text`), `input` (`voice` or `typed`), `reply` (`voice` or `text`) | How the owner talks to Pip now. Retained by the brainstem and replayed to every organ that joins; the bridge sends it again after a reconnect. `typed_voice`: typed lines, spoken replies with the words shown as they are played. `typed_text`: typed lines, no speech at all: the voice sends no audio but still reports each clause as played, confirmations and announcements are text, and alarms still ring. With `input: "typed"` the voice switches microphone capture off (not the mic mute), at once on receipt, and the mind shows text replies instead of asking for speech (host tests; not yet tried on the laptop). In `reply: "text"` there may be no voice to report playback (it can be down), so the mind itself reports each clause as played; how it does that (today the brainstem accepts `say.played` from the voice only) is that lane's change. Never saved: every boot starts `spoken`, and a guest forces `spoken`. An image can switch the written modes off with `"written_chat": false` in `being.json` (the chat bridge reads it at start): then `chat.mode` is only ever `spoken`, and every request for a written mode is refused with a `chat.notice`. If the bridge's connection drops, the brainstem tells everyone `spoken` again and replays that to organs that join. Survives a closed lid. |
| `chat.mode.request` | mind | `mode` (a mode name, or `next`) | The owner asked by voice ("Let's type", "Silent mode", "Back to talking") to change the mode. Only the mind may send it; the chat bridge applies it and answers with `chat.mode`. |
| `chat.notice` | chat bridge | `text` (up to 120 printable characters), `tone` (`warning` or `muted`) | A short line for the dock: why a typed line was not passed on (lid closed, Pip asleep or muted, not in a written mode). Not kept. |
| `camera.request` | eyes | `enabled` | Report the current owner permission after connecting or when it changes |
| `camera.policy` | brainstem | `allowed`, `seq`, `video_node` | Authorize capture for this epoch. On a mapped body, `video_node` is the kernel-verified primary V4L2 node number while granted. Unmapped bodies and closed grants carry null. New subscribers receive a snapshot. |
| `camera.look` | Mind → brainstem → Eyes | `id`, `lid_seq`, plus `camera_seq` on the forwarded request | Request one fresh frame during the current unmuted thinking turn. Brainstem allows one request per turn and directs it to the Eyes connection holding the grant. |
| `camera.look_cancel` | Mind → brainstem → Eyes | `id`, plus `lid_seq` on the Mind request | Cancel this request. Brainstem also cancels it on turn or grant change, timeout, or loss of the requesting connection. |
| `camera.look_result` | Eyes → brainstem → Mind | `id`, `lid_seq`, `status`, and `description` on success. Eyes also supplies `camera_seq`. | Returns private text of at most 240 UTF-8 bytes, or an unavailable result. Brainstem may return `disabled` for a missing grant. The result goes only to the requesting Mind connection. |
| `see.presence` | eyes | `present`, `camera_seq`, `lid_seq`, `x` and `y` when present | Latest normalized face center for the current policy and lid. Frames stay inside the detector. |
| `key.interrupt` | face, keyboard | | Esc was pressed, play/pause was pressed while the eyes were not resting or an app panel had the keyboard, or the owner typed over Pip in the typing chat |
| `key.mute` | face, mind | optional `on` | The microphone's mute key was pressed (M, or fn+F10 on the laptop). From the mind ("stop listening") it must carry `on: true`: on the laptop only a key or the menu turns listening back on. |
| `key.touch` | face | | A click or touch produces a brief downward gaze |
| `key.attend` | face | | Space, Return, play/pause or a trackpad click while the eyes rest (idle, drowsy or asleep with the lid open) and no app panel has the keyboard. The brainstem treats it like hearing its name, unless it is muted or the lid blocks it. The lab's typed console may send it too. |
| `key.panel_recover` | face | | F9 (fn+F9 on the laptop) restores the face when the lid reading is unavailable |
| `key.brightness` | face | `dir`: `up` or `down` | A brightness key (the printed F1 and F2). The mind sends `being.brightness` with `step`, `by: 6` and `req: "key"` |
| `key.kbd_light` | face | `dir`: `up`, `down` or `toggle` | A keyboard-light key (the printed F5 and F6, or a keyboard's own toggle key). The mind sends `being.kbd_light` with `req: "key"` |
| `key.volume` | face | `dir`: `up` or `down` | A volume key (the printed F11 and F12). The mind sends `voice.volume` with the next `level` on a Mac's sixteen steps (0, 6, 12, 19 ... 94, 100) and `feedback: true`. Stepping down to 0 is a speaker mute, as below |
| `key.mute_speaker` | face | | The speaker's mute key (the printed F10), not the microphone's. The mind sends `voice.volume` with `level: 0` and `persist: false`, and the saved level when it is pressed again. A ring or a battery plea brings the sound back first, and the next start of the mind puts the saved level back |
| `key.apps` | face | | Launchpad (the printed F4). The mind opens the workshop terminal as "open the terminal" does, brings back a minimised one, or minimises a shown one. Nothing is spoken |
| `voice.mute` | brainstem | `on` | Close or open the mic |
| `sim.body` | simulated body | any of `battery`, `charging`, `on_ac`, `temp`, `lux`, `lid_closed`, `health_pct`, `cycles` | Set a body reading by hand in the lab |

## Interrupted replies

The mind hands a reply to Voice clause by clause, usually faster than
Voice can speak it, so an interruption (Esc today, barge-in later) can leave
clauses queued that were never heard. Pip remembers saying only what played
(`mind/played.py`). This holds for a model reply and for a fixed reply
alike. A fixed reply is never in the conversation history, so for it only
its archive row, when it has one, and what Pip remembers having just said
follow what played:

- A clause counts as said once Voice reports its first audio (`say.played`
  with its `seq`). A clause stopped partway counts: its start was heard.
- An interrupted reply keeps the clauses that counted, in order, in the
  conversation history and the archive, marked as cut off (`—` in the
  history, `interrupted: true` in the archive). Clauses that never started
  are dropped. The reply keeps its `sources`.
- If nothing of it played, nothing is kept as said, and its question leaves
  the history too, as when nothing was handed to Voice.
- A reply Voice played to the end stays exactly as the model wrote it. A
  clause Voice could not play at all (a synthesis or speaker failure) is
  dropped in the same way, and the entry is marked cut off.
- The mind waits up to one second for Voice's final receipt after a cancel.
  It normally follows `say.stop` within milliseconds. A Voice that crashed
  never sends it, and the receipts already received decide. A closed lid
  decides at once. Receipts for a reply the mind has closed are ignored.
- Replies are written down before Voice has finished speaking them. If
  such a reply is stopped short, the mind corrects its history entry and
  archive row. The next turn waits for that correction. A fixed reply that
  carried the morning brief's reminders never had them archived, so only the
  leading clauses it did archive count: if they all played, its row stays
  as written.
- Several replies can be open at once, each settled by its own token.
- A Voice that has never sent `say.played` on this bus connection (an
  older Voice, or a stand-in) does not report playback. Its replies are
  kept as they were handed over, as before.

Face captions follow the same receipts. A reply's clause appears when
Voice reports it playing, and clauses still waiting when the reply's final
receipt arrives never appear. A clause without `reply` (the camera's
private description) appears when handed over. With a Voice that never
reports playback, a reply's clauses appear when its `say.done` arrives.
The typing chat (`keyboard`) follows the receipts the same way. It also
shows a reply twelve seconds after `mind.reply_done` if no Voice has
reported playback or begun speaking (`say.started`) yet. Once it has seen Voice go down,
it shows replies as they are handed over, because nothing can play them.
The same receipts are meant for barge-in.

## States

`waking`, `idle`, `attending`, `listening`, `thinking`, `speaking`, `drowsy`, `asleep`.
Two overlays ride on top: `muted` and `unwell`.

## Expressions

`neutral`, `attentive`, `listening`, `thinking`, `speaking`, `joy`, `curiosity`,
`surprise`, `skepticism`, `concern`, `sleepy`, `asleep`, `tired`, `muted`, `dazed`,
and the tuned expressions `sad`, `laugh`, `love`, `embarrassed`, `scared`,
`annoyed`, `smug`, `confused`, `wink`, `bored`, `relieved`, and the shock family
`shocked`, `startled`, `gasp`, `amazed`, `bewildered`, `speechless`, `horror`,
`stunned`, and the warm ladder `pleased`,
`excited`, `victorious`, `angry`, `disappointed`, `grateful`, `amused`,
`giggle`, `bashful`, `hopeful`, `encouraging`, `reassuring`, and the playful and warm
set `starstruck`, `proud`, `idea`, `crying`, `nervous`, `mischief`, `determined`,
`cheeky`, `sing`, `dizzy`, `ko`, `cozy`, `flirty`, `dreamy`, `adoring`, and the tense and
unhappy family `heartbroken`, `guilty`, `jealous`, `raging`, `disgusted`, `panicked`,
`freezing`, `lonely`, `pouting`, `grumpy`, `resigned`, `awkward`, and the Pip states
`p_processing`, `p_searching`, `p_glitch`, `p_success`, `p_music`, `p_scan`, `p_deadpan`,
`p_yawn`, `p_lowbat`, `p_wake`, `p_smug`, `p_sound` (names only: the face's own state poses
stay the real ones, and the director decides when each shows). `face.express` accepts
exactly these names; any other name is ignored. (`dreaming` is a demo-only pose
and `loading` is the face's own state before the brainstem first answers; neither
can be requested.) The model still chooses only from its seven tags
(`neutral`, `joy`, `curiosity`, `surprise`, `skepticism`, `concern`, `sleepy`);
the other names are sent only by the brainstem and the mind, and only when the
state behind them is true. Nothing shows while muted, dazed, asleep or dark.

Effects layer (off by default): with it on, a live pose shows only these marks,
as the expression itself authors them: blush, hearts, tears, sweat drops, the
sleep bubble, flame, steam, snow, and X or spiral pupils. Every other mark
(sparkle, zzz, exclaim, anger, notes, bulb, gloom, shiver, bounce, sway, glitch,
scan, check, warn) is lab-only, and so are the expressions `sick`, `queasy`,
`p_sneeze` and `p_hiccup`. The question mark, thinking dots and battery cue are
state-true and unchanged. Two guards: flame and steam never show while a fresh
`stem.budget` reading says `hot` or `critical` for a thermal reason (they show
where Bodycare gives no reading); X and spiral pupils never show while Pip is
unwell, link-lost or loading, nor for 5 s after. With the layer off every pose
is eyes-only.

What shows, and when:

| State | Expression | For | Who |
|---|---|---|---|
| `hear.missed`, `reason: no_speech` | `skepticism` | about 1.1 s | face |
| `hear.missed`, `reason: unclear` | `confused` | about 1.1 s | face |
| `term.status` `phase: failed` with a non-zero `exit` | `sad` | 1.5 s | brainstem |
| `term.status` `phase: done` with `exit: 0` (not `reason: exit_unknown`) | `relieved` | 1.5 s | brainstem |
| The charger goes in after a low-battery cue was showing | `relieved` | 2 s | face |
| Bodycare reports `hot` or `critical` pressure for a thermal reason for 30 s | `tired` cue, 6 s every 45 s while idle | until 60 s without such a reading | face |

The brainstem reacts to a job result only when the lid is open, the owner is
not a guest, and it is Idle or Attending, and it reacts once per job id and
phase. A status without a job id never reacts. The heat cue follows
`stem.budget` (the face subscribes to it; no new message): a reading counts
while its `ttl_ms` (at most 12 s) has not lapsed, and a reading up to 3 s late
still continues a run, because Bodycare republishes about every 10 s. A
revoke, a cooler pressure or another `priority` ends the run at once. A
low-battery cue takes precedence over the heat cue. It appears only where
Bodycare runs, so a lab image without Bodycare never shows it. This is not a
fault. The face keeps no temperature of its own, so the threshold is
Bodycare's own `hot_package_c` and moves with it.

The face plays the waking gesture on a confirmed lid opening. The low-battery
cue uses the `tired` pose during visible idle and clears when the body stops
reporting low battery. Overnight journal work keeps the panel dark. A live
`dreamed` opening adds a flutter to the wake blink after the first frame.
The sleeping flutter is available in the standalone face preview.

Workshop panels are shown and hidden by signalling the panel's console:
`systemctl kill --kill-whom=main --signal=SIGUSR1` (show) or `SIGUSR2`
(hide) on `being-panel-<app>.service`. The console does not read a
`visible=` key from its output.

Keys typed into a shown terminal panel do not cross the bus. The face sends
each one to hands as one datagram on hands' key socket: the bytes the key
types (US layout, the usual xterm sequences, Ctrl+letter), never Esc, which
stays `key.interrupt`. The panel's own keys go as control words, a NUL byte
and then a word: `\0f3` hides or brings back the terminal (F3, fn+F3 on the
laptop), and `\0pgup` and `\0pgdn` scroll its history (PgUp and PgDn).
Hands acts on these and never passes them to the shell. The printed
top-row keys (brightness, volume and the rest) are answered by the face
before a panel can see them, so they never reach the shell either.

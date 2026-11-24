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

## Topics

| Topic | Sent by | Fields | Meaning |
|---|---|---|---|
| `hello` | any organ | `name`, `sub` | Join the bus |
| `organ.up`, `organ.down` | brainstem | `name` | An organ joined or left |
| `stem.beat` | brainstem | `state`, `up` | Heartbeat, twice a second |
| `state.changed` | brainstem | `state`, `prev`, `muted`, `unwell` | The being's state or an overlay changed |
| `body.state` | brainstem | `battery`, `battery_low`, `charging`, `on_ac`, `temp`, `lux`, `lid_closed`, `hour`, `minute`, `health_pct`, `cycles` | Body readings. Unavailable hardware readings are null. `battery_low` is true at 15% or below with confirmed AC disconnection. `health_pct` is the full charge as a percentage of the design charge (81 on the test laptop), and `cycles` the battery's charge-cycle count. |
| `body.care` | bodycare | the care readings, plus `charge_limit` and `cap_honoured` | What bodycare is doing for the body. `charge_limit` is the charge cap it asked for (a percentage, or null). `cap_honoured` is true only when the battery has really stopped charging at that cap, so Pip never claims a cap that is not holding. |
| `body.lid` | brainstem | `closed` (boolean or null), `seq`, `show_face`, optional `snapshot` or `dreamed` | Only confirmed open permits capture. A retained snapshot catches a new subscriber up. While the lid reading is unknown, `show_face` can restore the panel locally. `dreamed` marks a live opening after completed journal work during that closure. |
| `body.presence` | brainstem | `present`, `lid_seq` | A presence reading from the current authorized camera. It carries no coordinates. Loss, timeout or revoked capture clears known presence. |
| `panel.ready` | face | `seq` | The first frame for this lid reading has been presented. The brainstem can restore panel power. |
| `body.senses` | brainstem | `line` | One line of true facts about the body, for the mind to show the model |
| `body.drives` | brainstem | `energy`, `arousal`, `social`, `comfort` | Slow internal numbers, 0 to 1 |
| `face.express` | brainstem | `name`, `hold` | Show an expression for `hold` seconds |
| `face.gaze` | brainstem | `x`, `y`, optional `hold`, or `present: false` | Track a point from -1 to 1, glance for `hold` seconds, or release the camera target |
| `hear.wake` | voice | | Its name was heard |
| `hear.speech_start` | voice | | Someone started talking |
| `hear.partial` | voice | `text` | Words so far; may be revised |
| `hear.final` | voice | `text`, `speaker` | The finished utterance |
| `hear.cancel` | voice | | False start, nothing was said |
| `hear.missed` | voice | `reason`: `no_speech` or `unclear` | It was listening and could make nothing out. The voice owns up itself ("Sorry, I didn't catch that.") once per wake, unless that wake was already answered with words (a cough in the follow-up window gets no apology); the mind only shows concern, and the face squints briefly. No words are carried. |
| `voice.level` | voice | `floor_dbfs`, `speech_peak_dbfs`, `gain`, `heard_ago_s`, `asr`, `tts` | How well it can hear: the room's noise floor and the last speech peak in dBFS, the software gain applied to the microphone, seconds since speech was last understood, and the recogniser and voice in use. Sent every 30 s while resting, and on request. Numbers and engine names only, never words. |
| `voice.level_request` | mind | | Ask the voice for a `voice.level` now ("How well can you hear me?") |
| `voice.volume` | mind | `level` (0 to 100) or `step`, optional `persist`, optional `feedback` | "Speak up", "be quieter", "set your volume to 5", or a volume or speaker-mute key. The voice owns the mixer and sets it. `persist: false` (a speaker mute from a key) sets the level for now only: the saved level in `voice.json` and the sound card's stored state stay as they were. `feedback: true` (the volume keys) plays one short click at the new level, never at 0, and only when a chirp could play and the microphone is not open for the owner's words |
| `voice.volume_state` | voice | `level`, `mixer` | What the voice really set, so the mind's answer is true. The face shows it as a level bar. After a key mute `level` is 0; the report the voice sends when the mind connects never is |
| `voice.chirps` | mind | `on` | Turn the chirps (earcons) on or off; the voice keeps the choice in `voice.json`. No spoken command sends it yet |
| `stem.chirp` | brainstem | `name` | Play one named chirp. The voice listens for it; the brainstem does not send it yet |
| `voice.engine` | mind | `tts`: `piper`, `flite` or `auto` | Choose the speaking voice. `auto` uses the natural voice (Piper, Alba) from the first reply and changes to the original voice (Flite) only if the natural voice fails to verify or load, or measures slower than 0.7 of real time on this laptop. The voice keeps the choice in `voice.json`. `being.tts=` on the kernel command line, or `BEING_TTS` in the voice's environment, overrides the choice for that boot. |
| `say.request` | mind | `text`, `seq`, `last`, optional `announcement`, optional `chirp` | Speak this clause. An empty final marker ends the reply and carries the attempt token for a reminder, timer or greeting. `chirp: "bell"` on the first clause of a ring (a timer, alarm or reminder) plays the bell before the words, when chirps are on, the lid is open and it is not muted. |
| `say.started` | voice | | First audio is playing |
| `say.level` | voice | `v` | Loudness of its own voice right now, 0 to 1 |
| `say.done` | voice | `final`, optional `announcement` | Queue is empty. `final` is true after the last clause. After successful announcement playback the voice echoes the attempt token. |
| `say.stop` | brainstem | optional `reason` | Stop speaking at once. `speak_timeout` indicates stalled playback. |
| `mind.ready` | mind | `cortex` | The mind is up; `cortex` says whether the model answers |
| `mind.emotion` | mind | `name`, `hold` | The emotion the model chose for its reply |
| `mind.reply_done` | mind | `spoke` | The reply is fully generated. `spoke` is false if nothing is to be said |
| `mind.cancel` | brainstem | optional `reason` | Abandon the reply in progress. `think_timeout` indicates a reply that failed to start. |
| `mind.announce` | mind | `announcement`, `lid_seq`, optional `idle_only` | Request a chance to speak with the current lid reading. First-daily greetings require the idle state. |
| `mind.dream_committed` | mind | `lid_seq` | Report a committed overnight batch with journal entries for the current confirmed closure. The brainstem consumes the receipt locally and acknowledges it only on the next confirmed opening. |
| `mind.clock` | mind | `utc_offset` (seconds east of UTC, or null) | The owner's time-zone offset now. Sent on every new bus connection and whenever it changes; the brainstem uses it for night, drowsiness and the senses line. Null means no zone is set, and the brainstem keeps UTC. |
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
| `app.open`, `app.close`, `app.minimise`, `app.restore` | mind | `app`: `terminal`, `monitor` or `clock`, `req` | Show, close, minimise or bring back one workshop panel. At most one app is shown; minimising a small app (monitor, clock) closes it. |
| `app.state` | hands | `app`, `view` (`shown`, `minimised` or `closed`), `session`, `job`, `req` | What the workshop now shows. A small app carries `session` and `job` as null. A small app whose renderer ends is announced closed without a request. |
| `app.refused` | hands | `req`, `reason`, `app`, optional `job` | The workshop would not do it (for example `busy`, `no_session`, `low_battery`, `too_warm`). `no_face`: the Try menu has the screen (`being-face.service` is not running and `being-local-screen.service` is), so a panel opened now could not be seen. The mind says "Close the Try menu to see my terminal." (or clock, or monitor), and a job that needed the terminal brought forward does not start. |
| `app.privacy` | mind | `guest` | A guest arrived (`true`) or left. Hands closes the session and clears the workshop; the voice keeps transcript text out of its log while a guest is here. The mind sends it again to a hands or voice that restarts while a guest is here. |
| `camera.request` | eyes | `enabled` | Report the current owner permission after connecting or when it changes |
| `camera.policy` | brainstem | `allowed`, `seq` | Authorize capture for this epoch, with a snapshot for new subscribers |
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

## States

`waking`, `idle`, `attending`, `listening`, `thinking`, `speaking`, `drowsy`, `asleep`.
Two overlays ride on top: `muted` and `unwell`.

## Expressions

`neutral`, `attentive`, `listening`, `thinking`, `speaking`, `joy`, `curiosity`,
`surprise`, `skepticism`, `concern`, `sleepy`, `asleep`, `tired`, `muted`, `dazed`.

A missed hearing (`hear.missed`) shows `skepticism` for about a second, a
"pardon?" squint. Nothing shows while muted, dazed, asleep or dark.

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

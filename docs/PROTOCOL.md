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
| `body.state` | brainstem | `battery`, `battery_low`, `charging`, `on_ac`, `temp`, `lux`, `lid_closed`, `hour`, `minute` | Body readings. Unavailable hardware readings are null. `battery_low` is true at 15% or below with confirmed AC disconnection. |
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
| `say.request` | mind | `text`, `seq`, `last`, optional `announcement` | Speak this clause. An empty final marker ends the reply and carries the attempt token for a reminder, timer or greeting. |
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
| `stem.announce_ready` | brainstem | `announcement` | The voice is free and the brainstem has entered thinking for the matching announcement. |
| `being.sleep`, `being.wake` | mind | | Go to sleep or wake up, on request |
| `camera.request` | eyes | `enabled` | Report the current owner permission after connecting or when it changes |
| `camera.policy` | brainstem | `allowed`, `seq` | Authorize capture for this epoch, with a snapshot for new subscribers |
| `see.presence` | eyes | `present`, `camera_seq`, `lid_seq`, `x` and `y` when present | Latest normalized face center for the current policy and lid. Frames stay inside the detector. |
| `key.interrupt` | face | | Esc was pressed |
| `key.mute` | face | | The mute key was pressed |
| `key.touch` | face | | A click or touch produces a brief downward gaze |
| `key.panel_recover` | face | | F9 restores the face when the lid reading is unavailable |
| `voice.mute` | brainstem | `on` | Close or open the mic |
| `sim.body` | simulated body | any of `battery`, `charging`, `on_ac`, `temp`, `lux`, `lid_closed` | Set a body reading by hand in the lab |

## States

`waking`, `idle`, `attending`, `listening`, `thinking`, `speaking`, `drowsy`, `asleep`.
Two overlays ride on top: `muted` and `unwell`.

## Expressions

`neutral`, `attentive`, `listening`, `thinking`, `speaking`, `joy`, `curiosity`,
`surprise`, `skepticism`, `concern`, `sleepy`, `asleep`, `tired`, `muted`, `dazed`.

The face plays the waking gesture on a confirmed lid opening. The low-battery
cue uses the `tired` pose during visible idle and clears when the body stops
reporting low battery. Overnight journal work keeps the panel dark. A live
`dreamed` opening adds a flutter to the wake blink after the first frame.
The sleeping flutter is available in the standalone face preview.

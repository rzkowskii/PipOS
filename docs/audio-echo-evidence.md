# Acoustic echo and barge-in evidence

This is preparation for external handover WP-E4 and WP-E15, not a production
audio feature. BeingOS remains half duplex: Voice stops capture while replying,
and only the isolated Ears service owns the microphone. The lab program neither
opens ALSA nor talks to Ears, Voice, the bus, or systemd.

WP-E4's least expensive candidate is a reference-aware acoustic gate: estimate
speaker-to-microphone delay and gain, require microphone residual for 150 ms,
then require the existing spotter to agree. Its native kill gate is more than
one false barge-in in 50 scripted interruptions. WP-E15 separately proposes
using known earcons to calibrate delay and gain. `lab/audio_echo_evidence.py`
implements only the offline measurement shared by those proposals.

## Inputs

For synthetic plumbing evidence, use the checked-in deterministic definitions:

```sh
python3 lab/audio_echo_evidence.py \
  --scenarios lab/audio-echo-scenarios.json >result.json
```

For a future explicitly authorised native session, provide three offline files:

```sh
python3 lab/audio_echo_evidence.py \
  --mic trace-mic.wav --reference trace-reference.wav --events trace-events.json
```

That command is deliberately unqualified for the E4 gate. A native acceptance
run also needs a separately authored `--labels trace-labels.json` document with
schema 1, a non-collector `source`, SHA-256 values for the exact `mic`,
`reference` and `events` files, and bounded non-overlapping `interruptions`.
The collector's empty label list can never qualify itself. The hashes bind
labels to trace bytes but do not authenticate their author, so even a complete
set reports `candidate`, never a final pass. A candidate result needs
at least 50 externally labelled interruptions, 49 matched detections, at most
one miss and at most one false candidate; otherwise it remains `insufficient`.

The production-compatible preparation route is deliberately narrower than
native duplex. With the lid confirmed open and the brainstem heartbeat live:

```sh
pip-diag echo-trace --consent-room-audio \
  --out /run/being-diag/echo-trace
python3 lab/audio_echo_evidence.py \
  --mic /run/being-diag/echo-trace/echo-mic.wav \
  --reference /run/being-diag/echo-trace/echo-reference.wav \
  --events /run/being-diag/echo-trace/echo-events.json
```

The diagnostic reuses `AudioSession`, its run lock, signal cleanup and wrapped
`ExecStopPost=... restore`. It temporarily stops Voice and Ears and restores
their prior states. It checks the lid and heartbeat throughout the trace.
Room audio requires the explicit flag and a fresh private directory directly
under `/run/being-diag`. In-progress capture uses an anonymous RAM file.
The finished files disappear at reboot. The manifest records
`host_scheduled_unmatched` timing based on the separate
`arecord` and `aplay` request clocks.

For native clock evidence, use the separately consented diagnostic:

```sh
pip-diag native-echo-trace --consent-room-audio \
  --out /run/being-diag/native-echo-trace
```

This invokes a diagnostic-only `libasound` helper after Voice and Ears have
released the card. It requires exact 48 kHz stereo S16, 10 ms periods, a
four-period buffer, `MONOTONIC_RAW` ALSA timestamps, and a successfully linked
capture/playback group on `hw:0,0`; it fails closed when the hardware cannot
provide those constraints. The manifest records the shared start clock,
capture/playback ALSA trigger and status clocks, their trigger delta, frame
counts, buffer geometry, selected capture channel, the exact four-pulse
reference layout and xrun count. Schema 4 also snapshots the real playback
handle immediately after linked start: its `MONOTONIC_RAW` status and trigger
clocks, initial queued frames, kernel delay and available frames. The analyzer
reports these under `kernel_queue_observation`; they describe ALSA's queue at
one observation instant, not when speaker amplitude physically began. Linking gives
the two streams one ALSA start group and clock domain; it does not by itself
prove that the capture and playback converters have zero long-run sample-clock
drift, so offline acoustic alignment remains required. Lid, heartbeat and the microphone breach latch
are polled throughout; revocation terminates the helper and removes every
partial trace before the normal audio services are restored. This evidence
route does not enable duplex in Voice or Ears.

The offline report validates that the reference PCM has signal only inside the
declared pulse windows. For a native trace it adds `acoustic_onset`: the first
reference pulse frame, its frame-derived `MONOTONIC_RAW` time, the correlated
capture frame, delay and correlation; `calibration` separately reports gain.
`estimated` requires a correlation
of at least 0.2; otherwise the status is `not-detected` and no delay is
reported. Correlation searches a one-millisecond grid, so delay has one-
millisecond measurement resolution. The timestamp is explicitly a linked-trigger-plus-frame estimate,
not a claim to a physical DAC or ADC timestamp. Native MacBook evidence is
still required before this can calibrate any live gate.

For WP-D7 this is deliberately observation rather than a Voice change. A
future gaze lead may be compared with the kernel queue estimate, but it cannot
be promoted until the microphone correlation repeatedly establishes physical
onset on the MacBook and the production `aplay` path is measured separately.
No fixed lead or new playback delay is inferred from this diagnostic stream.

WAVs must be mono PCM16 at the same rate. The reference is the exact PCM handed
to playback, not audio re-recorded from the room. Event times are milliseconds
on the same monotonic origin used to start those streams:

```json
{
  "schema": 1,
  "name": "native-01",
  "frame_ms": 10,
  "max_lag_ms": 250,
  "hold_ms": 150,
  "residual_rms_threshold": 0.025,
  "playback": [{"start_ms": 500, "end_ms": 8500}],
  "interruptions": [{"start_ms": 2400, "end_ms": 3100}]
}
```

One trace is bounded to 120 seconds and alignment uses at most its first 30
seconds. Split a 50-interruption session into named traces rather than feeding
an unbounded recording. `interruptions` are ground-truth owner speech labels;
unlabelled room noises therefore correctly count as false acoustic candidates.

An optional `--processed candidate.wav` reports echo-return loss enhancement
(ERLE) in labelled playback-only frames. It does not endorse or install that
processor. Output contains no PCM or speech text, only lag, correlation, gain,
counts, latency, fixture expectations, and optional ERLE. The E4 result remains
`insufficient` until a trace set contains at least 50 labelled interruptions.

## Native acceptance dependency

The opt-in native helper now prepares the linked clock/reference evidence. Its
exact constraints still require validation on the MacBookPro9,2; an unsupported
ALSA link is a skip, not permission to infer synchronization. Before any
production seam, run 50 scripted interruptions on the laptop and combine this acoustic candidate
with the real wake/command spotter. Reject the reference-aware gate if it has
more than one false barge-in, or if capture/playback xruns, privacy revocation,
or wake cancellation regress. AEC3 remains a later candidate, not a fallback
silently enabled by this tool.

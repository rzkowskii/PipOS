# WP-E16 Face motion audition

Use this offline Face Lab comparison from an explicit development or Try
audition.

All four opaque arms last 48 seconds under the native scene clock. Seconds
0–24 are the rest condition: the same neutral pose and about 17 blinks/minute
in every arm. Seconds 24–48 are a stylised conversation proxy. They use the
same checked expression endpoints—Concern/Joy for valence and Sleepy/Surprise
for arousal—alternating every six seconds.

The four arms vary cadence and blending independently.

- `motion-arm-a` uses about 17 blinks/minute and discrete endpoint changes.
- `motion-arm-b` uses about 26 blinks/minute in the conversation half and discrete
  endpoint changes.
- `motion-arm-c` uses about 17 blinks/minute and smoothstep interpolation.
- `motion-arm-d` uses about 26 blinks/minute in the conversation half and smoothstep
  interpolation.

Arm A provides the synthetic reference for comparison. Compare A with B
and C with D for cadence. Compare A with C and B with D for interpolation.

For a blind owner session, the operator should randomise presentation order,
record only the opaque arm letter, and reveal the mapping after the owner
has given a preference. Keep every other option identical.

```text
face --script motion-arm-a --seed 1616 --size 1280x800 --style eyes
face --script motion-arm-b --seed 1616 --size 1280x800 --style eyes
face --script motion-arm-c --seed 1616 --size 1280x800 --style eyes
face --script motion-arm-d --seed 1616 --size 1280x800 --style eyes
```

For a later off-screen comparison, add the same `--frames DIR --fps 30` and a
different empty output directory to each command.

The second half uses scripted mood changes as a visual proxy for
conversation. Use the paired comparisons to identify which factor drives a
preference. Use an owner-visible Try audition with live conversation to
evaluate production behavior.

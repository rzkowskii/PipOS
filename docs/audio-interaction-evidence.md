# Offline audio interaction evidence

`lab/audio_interaction_evidence.py` is a default-disconnected preparation tool
for WP-E15. It reads only PCM16 WAV files explicitly supplied in a manifest and
produces aggregate, text-free measurements. Pitch, energy and cadence can still
correlate with a voice, so the report is private evidence rather than anonymous
telemetry. The tool performs no identity scoring or enrolment. It cannot
capture audio, contact Voice or Ears, change the recognizer, or enable fan
subtraction or model tags.

The manifest is intentionally explicit:

```json
{
  "schema": 1,
  "provenance": "synthetic",
  "clips": [
    {"role": "fan-silence", "wav": "fan-2000.wav", "fan_rpm": 2000,
     "fan_rpm_source": "hardware-sampled"},
    {"role": "speech", "wav": "synthetic-voice.wav", "syllable_count": 12},
    {"role": "control", "wav": "quiet.wav"}
  ]
}
```

`provenance` must be `synthetic` or `owner-consented`. Results must go to an
explicit new `--output` file, created mode 0600; nothing is printed to stdout.
Inputs are no-follow regular-file snapshots, and WAV paths must be relative
beneath the manifest without symlink components. A run accepts at most 12
clips, 120 seconds per clip and 600 seconds in total. WAVs are exact PCM16 at 8
or 16 kHz; truncated declared PCM is rejected. Stereo input is reduced to
the higher-energy channel and that decision is recorded. Output identifies
inputs by SHA-256, not filename, and contains only aggregate energy percentiles,
pitch quartiles, voiced/activity fractions, activity bursts, and three broad
spectral-band fractions. It emits neither PCM, transcript, word timing, a pitch
contour, nor an embedding. These voice-correlated aggregates remain private
development measurements, not a speaker identity or health/emotion inference.

Energy and activity inspect every 20 ms step. To keep the declared duration
bound computationally meaningful, pitch uses at most 600 evenly spaced active
frames and broad-band spectra use at most 64 evenly spaced frames; both sample
counts are included in the report.

Activity bursts are explicitly **not speech rate**. If a synthetic fixture or
consented protocol supplies a non-content `syllable_count`, the report may also
show syllables per detected active second and records that the count came from
the manifest. The analyzer never derives words or syllables from private audio.

A `fan-silence` clip may bind a bounded `fan_rpm` to an explicitly
`hardware-sampled` or `synthetic-fixture` source. It supplies only a comparison
baseline for the 50–170 Hz band
where the existing native diagnostic observed the laptop's fan/hum hump. This
does not learn a per-RPM filter: the RPM is a descriptive input, not a verified
control signal, and the tool emits no processed audio. A later native trial
must bind measured RPM and a silence interval, then compare wake false triggers
and WER at 2000 and 3500 RPM. WP-E15 requires killing fan subtraction on any WER
increase. No result from this tool satisfies that gate or authorizes a change to
the shipped 170 Hz high-pass.

Likewise, pitch/energy/rate must not be sent to the model or face until an
owner-approved vocabulary, expiry rule, false-tag review and native benefit are
defined. This tool leaves every production default unchanged and reports native
acceptance as `not-run`.

## Fan-subtraction candidate pairs

`lab/fan_spectral_candidate.py` is the deliberately offline next step for the
WP-E15 subtraction proposal. It requires the same explicit provenance and
private, pinned WAV inputs. Every speech clip and fan-silence reference carries
a bounded descriptive `fan_rpm` and evidence source. A speech clip must have
exactly one same-rate reference at the same RPM.

The program creates a new mode-0700 directory containing generic, mode-0600
paired mono WAVs: the selected-channel original and a Hann-windowed spectral
subtraction candidate with identical sample rate and frame count. `pairs.json`
binds both files to their input/reference hashes and records the transform.
It contains no transcript and performs no recognition.

These are comparison inputs, not improved audio. The next dependency is to run
the shipped recognizer and wake spotter over both sides of a consented native
corpus at 2000 and 3500 RPM. Any WER increase kills production subtraction;
until that paired result exists, `production_promotion` remains false and the
normal microphone path is unchanged.

Candidate manifests additionally bind every clip to `expected_sha256`, an
opaque `capture_group` (`a-z`, digits, `_` or `-`, at most 32 characters), and
the explicitly selected `channel` (0 or 1). Speech and fan reference must match
all three acquisition dimensions: group, selected channel and sample rate, as
well as RPM and RPM evidence source. Unknown clip fields, unknown roles and
duplicate speech bytes are rejected. A fan-silence reference is at least two
seconds long. Duplicate selected-channel PCM is also rejected even if it was
wrapped in a different stereo WAV, and the learned magnitude profile uses only complete interior
512-sample windows—zero padding is used only at the speech output boundaries.

Output creation walks every parent with no-follow directory descriptors, then
creates and pins the new directory by descriptor. Its owner and exact 0700 mode
are checked before and after each write; every output is also verified as a
new, single-link, owner-held regular file with exact mode 0600. `pairs.json` reports the
reference group, channel, rate, frames, duration, RPM provenance and number of
complete profile windows used.

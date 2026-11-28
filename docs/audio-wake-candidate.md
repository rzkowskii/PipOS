# Sherpa wake candidate comparison

WP-E5 does not change the normal wake engine. PocketSphinx remains the shipped
spotter, and CAM++ remains a separate, consent-gated speaker experiment.

`lab/wake_candidate_compare.py` compares offline qualification evidence. It
compares paired results from the same synthetic clips without opening audio,
loading an engine, or accepting owner recordings. The evidence packet must be
`pipos-wake-paired-evidence-v1`, contain 2–512 cases, and contain exactly one
real result from each engine for every case. Case IDs are non-private tokens;
no paths, phrases, PCM, speaker names, or transcripts belong in the packet.

Each case has:

- `id`, `synthetic: true`, and duration `seconds`;
- `condition`: `wake`, `near_miss`, `unaddressed`, or `noise`;
- `expected: true` exactly for `wake`.

Each engine has `provenance`, `inputs`, and `results`. Both provenance blocks and the
packet carry the same content-derived `corpus_sha256`; differing hashes are
unpaired and refused. Provenance pins PocketSphinx 5.0.4
or sherpa-onnx 2.0.3 to the source archive already pinned by Buildroot and
requires the installed binary digest. Sherpa additionally requires
`api: sherpa-onnx-c-api`, the model descriptor digest, an explicit
`model_trust` of `lab_digest_only` or verified `lab_detached_cms`, and digests
for the encoder, decoder, joiner and tokens. Each input echoes the exact
`{id, frames, sample_rate: 16000, wav_sha256}`. Both runners must report
consuming the same complete audio. A result carries `id`, `consumed_frames`,
`detected`, `trigger_audio_ms`, `processing_ms` and `score`. Trigger timing is
audio consumed to a hit and is null for a miss. Processing is whole-clip wall
time. The score can be null. Missing, duplicated and partial results are refused.

`lab/sherpa_kws_candidate.py` is the isolated v2.0.3 C-layout adapter. It
requires an explicit four-file transducer descriptor (`encoder`, `decoder`,
`joiner`, `tokens`) and a synthetic WAV corpus. It verifies all bytes, uses the
official keyword-spotter ownership rules, appends the documented 0.3-second
tail, and emits the comparator engine block. With no signature options it
labels evidence `lab_digest_only`. `--cms descriptor.cms --certificate
lab-ca.pem` verifies a detached DER CMS over the exact descriptor and labels
it `lab_detached_cms`; that label is still lab trust, never release signer
trust. The model payload is deliberately not included in this repository.
The descriptor must name the official
`sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01-mobile` candidate and its
published archive SHA-256
`2e6ac2577310bfa2f4b6b5fab0478b868c9d0b2cb2c51b3e13b50581b588864d`;
the adapter additionally hashes every extracted component rather than trusting
the archive label.

The PocketSphinx runner uses Pip's production recognizer and hashes the local
acoustic model, dictionary, language model and effective configuration. Run
the two adapters against the same synthetic corpus to produce the comparison.

```sh
python3 -m lab.sherpa_kws_candidate --library /path/to/libsherpa-onnx-c-api.so \
  --descriptor kws-pack.json --corpus synthetic-corpus.json > sherpa.json
python3 -m lab.pocketsphinx_wake_candidate --library /path/to/libpocketsphinx.so.5.0.4 \
  --corpus synthetic-corpus.json > pocket.json
python3 -m lab.wake_candidate_compare --corpus synthetic-corpus.json \
  --pocketsphinx pocket.json --sherpa sherpa.json
```

The report gives both confusion matrices, triggered negative clips per audio
hour, false-reject rates, true-positive trigger times, whole-clip processing
times and paired disagreements. The
small synthetic result is descriptive only, even when Sherpa is no worse on
either error count and strictly better on at least one. `acceptance_gate` and
`production_promotion` are always false: a preregistered adequately sized
held-out corpus, the WP-E5 owner-recording choice, native 0.5/1.5/3 m trials, and room
false-wake measurement remain separate explicit gates.

The lab adapter binds the v2.0.3 keyword-spotter API separately from the live
Voice bindings. Digest-only files and verified lab CMS have distinct trust
labels. A signed production model pack and native results remain release gates.

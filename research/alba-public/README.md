# Preparing the public Alba retrain

`prepare.py` converts the pinned Piper retrain for a local Sherpa trial.
Stage the files named in `PINS` at their recorded revision, using the
script's local filenames. Supply a new output directory.

```sh
python3 research/alba-public/prepare.py /path/to/pinned-inputs /path/to/new-trial
```

The converter checks SHA-256 and the corresponding Git blob identifiers.
It preserves the weights and phone IDs and adds Sherpa metadata, the
compatible token table and the alias map that PipOS reads from the voice
card. It keeps the original card and source notices beside the prepared
files, along with an output hash manifest.

## Prepare build inputs

Stage an input set from the repository root using the pinned eSpeak NG ZIP
in [the source notes](NOTICES.md). The builder needs CMake and a native C
compiler. The regular PipOS builder has both.

```sh
python3 -m lab.alba_retrain --prepared PREPARED_ALBA \
  --espeak-source ESPEAK_SOURCE_ZIP --output ALBA_INPUT
```

The command verifies the ZIP before extracting it and runs its CMake `data`
target. It then checks the six pinned Alba files and creates a reproducible
archive with the generated pronunciation data and notices. The `pack.json`
descriptor retains the pinned source revision and checksum. For data built
separately, `--espeak-data` accepts the generated directory. In the isolated
release checkout, place the archive at the descriptor's path under `out`,
merge the generated entry into `out/voice/DOWNLOADS.json`, and use the
descriptor as `os/package/being/model-packs/tts/pack.json`. Buildroot embeds
that descriptor in the system slot and uses its normal signing step for
the models partition. Use `lab.hardware_image --require-alba-retrain` on the
resulting image to verify the packaged voice bytes.

The 26 November ARM host witness checked ten Piper phoneme sequences through
the production loader and Sherpa 2.0.3. The notices now carry Edinburgh's
requested citation and rights statement, and hashes for the staged Piper
Phonemize and eSpeak NG source archives. Use the pinned eSpeak source to
produce the matching pronunciation data for release. The pack still needs
the full voice permission notice, Linux Ivy Bridge synthesis and listening
checks, and an approved signed descriptor. [Rights and source notes](NOTICES.md)

# Public Alba retrain attribution and local release review

Voice model `en_GB-alba-medium`, distributed by the Piper voice contributors
in `rhasspy/piper-voices`. The upstream repository README marks the voices
MIT. The source is commit `cf910fb59f1d79ab3b625a63993a108751c6c25d`.
The original voice card and repository README are retained with this
conversion. The pinned root tree and Alba voice directory contain the MIT
declaration in the README and the dataset licence in the card. They contain
no separate MIT copyright or permission notice. [Pinned public voice and card](https://huggingface.co/rhasspy/piper-voices/tree/cf910fb59f1d79ab3b625a63993a108751c6c25d/en/en_GB/alba/medium)

Valentini-Botinhao, Cassia and Yamagishi, Junichi (2019). *Alba speech
corpus* [dataset]. University of Edinburgh, Centre for Speech Technology
Research. DOI `10.7488/ds/2506`. [Dataset and requested citation](https://doi.org/10.7488/ds/2506)

The Edinburgh record licenses the corpus under Creative Commons Attribution
4.0 International and states that the licence does not affect the voice
talent's moral rights. It prohibits use derogatory to the voice talent and
references the rights language in section 2(b)(1) of the licence. The
original corpus credits apply to the Alba fine-tune. [Edinburgh rights statement](https://datashare.ed.ac.uk/handle/10283/3270) · [CC BY 4.0 legal code](https://creativecommons.org/licenses/by/4.0/legalcode)

The public retrain uses `hifi_tts_92` as its base. Its voice is **Cori
Samuel**, Hi-Fi TTS reader 92, from LibriVox public-domain audiobooks.
The base model uses the Hi-Fi Multi-Speaker English TTS Dataset, NVIDIA
Corporation, CC BY 4.0. The original base voice card is retained.
[Base voice](https://huggingface.co/rhasspy/piper-voices/tree/cf910fb59f1d79ab3b625a63993a108751c6c25d/en/en_US/hifi_tts_92/medium) ·
[Dataset](https://www.openslr.org/109/) ·
[Bakhturina, Lavrukhin, Ginsburg and Zhang, Interspeech 2021](https://arxiv.org/abs/2104.01497)

The local conversion adds Sherpa metadata and uses private-use scalars in
its compatibility token table. The ONNX graph, model weights and phone IDs
are preserved. The prepared runtime card includes the alias map and keeps
a separate copy of the original Piper config. The inputs are limited to the
pinned Alba retrain and attribution documents.

The planned production phonemizer is Piper Phonemize 1.5.9, maintained by
k2-fsa, by Michael Hansen and contributors. It statically includes eSpeak
NG (the eSpeak NG contributors, based on Jonathan Duddington's eSpeak),
Unicode data and uni-algo. The phonemizer, eSpeak NG and pronunciation
data have GPL terms and corresponding-source requirements. See the BeingOS
`os/package/piper-phonemize/LICENSES.md` for the pinned source versions,
build instructions and additional data notices. No runtime or pronunciation
data are redistributed in this draft. [Piper Phonemize 1.5.9](https://github.com/csukuangfj/piper-phonemize/tree/v1.5.9) ·
[eSpeak NG source](https://github.com/csukuangfj/espeak-ng/tree/ed530aa113046142eb5115cf2fc9157854d0ffe1)

## Corresponding source archives

The v1.5.9 tag resolves to commit `dae4d2d831f9f0817355f7c0e8abd98c2fb0a49d`.
Its CMake file pins the eSpeak NG revision and archive checksum below.
Both archives were downloaded and hashed on 26 November 2026. The upstream
Linux shared-library release packs `lib/` and `include/`. The voice pack
still needs pronunciation data generated from the identified eSpeak NG
source for its production candidate. The existing private pack uses a
separate Sherpa archive. [Pinned build workflow](https://github.com/csukuangfj/piper-phonemize/blob/dae4d2d831f9f0817355f7c0e8abd98c2fb0a49d/.github/workflows/build-rust-libs.yaml)

| Archive | Bytes | SHA-256 |
|---|---:|---|
| `piper-phonemize-v1.5.9.tar.gz` | 10375252 | `71a8a46b29ef74642e8fd3e940fdabac8d8cdd56d302c698cc5763334ab323c4` |
| `espeak-ng-ed530aa113046142eb5115cf2fc9157854d0ffe1.zip` | 18011501 | `e4e262cbe34f7fe21f91f1ba3397f2728e1f30eafbae7853f2b753a9ed13f0dd` |

Before publishing an image, resolve the full applicable voice copyright and
permission notice and attach the corresponding phonemizer source archives
with the image's licence materials. The source and attribution were reviewed
locally. Production packaging and the Ivy Bridge audition are still pending.

# Speech engine licences and credits

This folder is installed as `/usr/share/licenses/sherpa-onnx/` on PipOS. It
covers the speech libraries and the voice and hearing models they run.

## Libraries (this package)

| Part | Version | Licence | Text |
|---|---|---|---|
| sherpa-onnx C API (`libsherpa-onnx-c-api.so`) | v2.0.3 (fallback v2.0.2), the upstream `linux-x64-shared` release | Apache-2.0 | `LICENSE-sherpa-onnx-Apache-2.0` |
| ONNX Runtime (`libonnxruntime.so`), bundled in that release | 1.28.2 | MIT, Copyright (c) Microsoft Corporation | `LICENSE-onnxruntime-MIT` |
| espeak-ng, linked statically into the sherpa-onnx library through piper-phonemize for Piper phonemisation | the revision pinned by the sherpa-onnx release | GPL-3.0-or-later | `COPYING-espeak-ng-GPL-3.0` |

sherpa-onnx is Copyright (c) Xiaomi Corporation and its contributors
(https://github.com/k2-fsa/sherpa-onnx).

Because the TTS-enabled sherpa-onnx library contains espeak-ng, distributing
it carries the GPL-3.0 obligations. The corresponding source is the
sherpa-onnx source release of the same tag
(https://github.com/k2-fsa/sherpa-onnx/releases/tag/v2.0.3) together with the
espeak-ng revision its CMake files pin. PipOS's own source is GPL-3.0 as well
(the repository's `LICENSE`).

ONNX Runtime publishes the notices for its own third-party components in
`ThirdPartyNotices.txt` at https://github.com/microsoft/onnxruntime (tag
v1.28.2). The release archive this package installs does not contain that file.

## Models (installed by the model packs, credited here)

| Model | Used for | Licence | Credit |
|---|---|---|---|
| Silero VAD (`silero_vad.onnx`) | finding where speech starts and ends | MIT | Silero Team, https://github.com/snakers4/silero-vad |
| Moonshine v2 tiny and base, English | turning speech into words after the wake phrase | MIT | Useful Sensors / Moonshine AI, https://github.com/moonshine-ai/moonshine |
| Piper voice `en_GB-alba-medium` | the natural voice | voice model MIT (rhasspy/piper-voices) | Michael Hansen and the Piper contributors |
| espeak-ng-data (inside the voice folder) | pronunciation data for the natural voice | GPL-3.0-or-later | the espeak-ng contributors |

### Alba dataset (CC BY 4.0)

The natural voice was trained on the Alba speech dataset:

> Alba: Scottish English speech data, Centre for Speech Technology Research
> (CSTR), University of Edinburgh. https://datashare.ed.ac.uk/handle/10283/3270
> Licensed under the Creative Commons Attribution 4.0 International licence,
> https://creativecommons.org/licenses/by/4.0/

The voice's model card says it was fine-tuned from Piper's U.S. English
`lessac` medium voice.

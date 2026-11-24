# Phonemizer licences and source

This folder is installed as `/usr/share/licenses/piper-phonemize/` on PipOS.
It covers the library that turns text into phonemes for the natural voice.

| Part | Version | Licence | Text |
|---|---|---|---|
| piper-phonemize (`libpiper_phonemize_core.so`), the upstream `linux-x64-shared-lib` release | 1.5.9 | GPL-3.0-or-later | `COPYING-GPL-3.0` |
| espeak-ng, linked statically into that library | the revision pinned by piper-phonemize 1.5.9 (commit `ed530aa113046142eb5115cf2fc9157854d0ffe1` of https://github.com/csukuangfj/espeak-ng) | GPL-3.0-or-later | `COPYING-GPL-3.0` |
| ucd-tools Unicode character data, inside espeak-ng | as above | Unicode Data Files licence | `COPYING-espeak-ng-UCD` |
| uni-algo (Unicode normalisation), compiled into piper-phonemize | the copy in piper-phonemize 1.5.9 | Unlicense or MIT | `LICENSE-uni-algo` |
| espeak-ng-data (pronunciation data) | ships in the voice pack on /models, not in this package | GPL-3.0-or-later | `COPYING-GPL-3.0` |

piper-phonemize is by Michael Hansen and its contributors; the release with a
C API is maintained by the k2-fsa project (https://github.com/csukuangfj/piper-phonemize).
espeak-ng is by the eSpeak NG contributors, based on eSpeak by Jonathan
Duddington (https://github.com/espeak-ng/espeak-ng).

## Corresponding source

The library is distributed as object code, so its corresponding source must
be available under GPL-3.0 section 6. PipOS's own source is GPL-3.0 as well
(the repository's `LICENSE`). The corresponding source is:

- piper-phonemize at tag `v1.5.9`:
  https://github.com/csukuangfj/piper-phonemize/archive/refs/tags/v1.5.9.tar.gz
- espeak-ng at commit `ed530aa113046142eb5115cf2fc9157854d0ffe1`:
  https://github.com/csukuangfj/espeak-ng/archive/ed530aa113046142eb5115cf2fc9157854d0ffe1.zip
  (sha256 `e4e262cbe34f7fe21f91f1ba3397f2728e1f30eafbae7853f2b753a9ed13f0dd`,
  as pinned in piper-phonemize's `CMakeLists.txt`)
- the build: piper-phonemize's `.github/workflows/build-rust-libs.yaml`, job
  `build-linux-x64-shared` (CMake with `-DBUILD_SHARED_LIBS=ON` in the
  manylinux2014 x86_64 container).

A release that offers PipOS images must also offer copies of these archives
next to the images, for as long as the images are offered, so the source does
not depend on the upstream hosts staying up.

# Stock speech bank preparation

WP-D6's build-time bank contains the eight exact, product-authored
`STOCK_LINES` clauses defined in `mind/tts.py`.

`lab/build_stock_speech.py` takes explicit local paths to the shipping Piper
voice, sherpa-onnx library and Piper phonemizer, plus the normal checked Pip
profile. The profile supplies the exact supported speech rate. It uses one
synthesis thread, enforces eight reviewed clauses, validates bounded mono
16-bit WAV output, and writes content-addressed clips plus `bank.json` through
an atomic new-directory rename. The manifest binds every clip to its clause
hash, rate, PCM geometry, runtime libraries and direct model/card/token bytes.

For an installed `/models` voice, the builder requires the existing signed
model-pack verifier to accept the pack and records
`runtime_signed_pack_verified`. For an offline staging tree, pass
`--descriptor` with its TTS `pack.json` and `--certificate` with the trusted
signer certificate. The normal TTS role directory must hold its CMS. A
descriptor-only build records local digest evidence for development.

The builder uses the exact Linux runtime shipped in the intended image.

```sh
python3 -m lab.build_stock_speech \
  --voice-dir /models/voice/piper/default \
  --library /usr/lib/libsherpa-onnx-c-api.so \
  --phonemizer-library /usr/lib/libpiper_phonemize_core.so \
  --factory-config /etc/being --profile-dir /mind \
  --output out/stock-speech
```

Stage the approved bank in `out/stock-speech` and enable
`BR2_PACKAGE_BEING_STOCK_SPEECH` for the image. The post-build step validates
and copies the eight clips to `/usr/share/being/stock-speech` in the signed
system slot. The source snapshot includes the staged bank. A bank rendered from the
research-only Lessac-derived Alba voice must never go into a public image
or source snapshot; regenerate it from the public Alba retrain first. Voice prepares it
off the event loop once the active engine is known.
The loader verifies the read-only system origin, active signed voice, exact
speech rate, runtime binaries, model files, eSpeak tree and all eight WAVs.
On a RAM-cache miss, a ready clip passes through the normal gain and speech
filter. An absent, pending or refused bank lets normal synthesis proceed.
Diagnostics count RAM and bank hits separately. Stock clip generation and
native listening and first-audio measurements remain open.

# Closed-eyes boot splash preparation

The boot-splash candidate is not a copied shader or a hand-drawn likeness.
`lab/closed_eyes_splash.py prepare` asks the exact `being-face` binary under
qualification to render the production `asleep` expression at the laptop's
1280×800 layout through Face's existing snapshot path:

```sh
python3 lab/closed_eyes_splash.py prepare \
  --face /absolute/path/to/being-face \
  --output out/closed-eyes/canonical.png \
  --manifest out/closed-eyes/canonical.json
```

The snapshot path uses the shipped expression catalogue, palette, OpenGL/GLES
shader, capture orientation, and full-face layout. Optional decorative and
effect layers stay off, as they do for the ordinary eyes-only face. The
manifest binds the PNG to the renderer binary and exact fixed command. Both
outputs must be new, so evidence from an earlier build is not overwritten.

After native evidence captures the first and last firmware/simpledrm/i915/Face
pixels as PNG or binary RGB PPM, compare them without image-library
dependencies:

```sh
python3 lab/closed_eyes_splash.py compare \
  --first out/closed-eyes/first.ppm \
  --last out/closed-eyes/last.ppm \
  --canonical out/closed-eyes/canonical.png \
  --manifest out/closed-eyes/canonical.json \
  --face /absolute/path/to/being-face \
  --output out/closed-eyes/pixels.json
```

The report includes exact hashes, changed-pixel fraction and bounding box,
maximum and mean channel delta, both first-to-last and optionally against the
canonical frame. Exact comparison is the default; `--threshold N` may
separately document small driver colour differences without changing hashes.
Supplying the manifest also verifies that the canonical PNG and optional
renderer binary are still the exact build used by `prepare`.

This is preparation only. It does not install the PNG, alter GRUB, enable a
graphics payload, change simpledrm/i915 handoff, or claim flicker-free boot.
Promotion needs the native first-to-last capture showing that the chosen boot
path preserves or replaces pixels as intended.

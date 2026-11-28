#!/usr/bin/env python3
"""Generate the standalone Face Lab preview or check it for source drift."""

import argparse
import json
from pathlib import Path
import runpy

HERE = Path(__file__).resolve().parent
SHELL = HERE.parent / "shell-installer-poc"
MARKER = "/*@@FACE_BUNDLE@@*/"


def render_page():
    shared = runpy.run_path(str(SHELL / "build.py"))["face_bundle"]()
    bundle = {key: shared[key] for key in ("header", "vert", "frag", "look", "presets", "sources")}
    renderer = "\n".join((SHELL / "src" / name).read_text(encoding="utf-8")
                          for name in ("face-gl.js", "face-anim.js"))
    adapter = (HERE / "src/device-eyes.js").read_text(encoding="utf-8")
    source = (HERE / "src/face-lab.src.html").read_text(encoding="utf-8")
    if source.count(MARKER) != 1:
        raise ValueError("Face Lab source needs exactly one shader bundle marker")
    compiled = f"const DEVICE_FACE = {json.dumps(bundle, separators=(',', ':'))};\n{renderer}\n{adapter}"
    return source.replace(MARKER, compiled.replace("</script", "<\\/script"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    page = HERE / "index.html"
    generated = render_page()
    if args.check:
        if not page.exists() or page.read_text(encoding="utf-8") != generated:
            raise SystemExit("Face Lab is out of date. Run python3 build.py.")
        print("Face Lab matches the preview source and production shader.")
    else:
        page.write_text(generated, encoding="utf-8")
        print(f"Wrote {page}")


if __name__ == "__main__":
    main()

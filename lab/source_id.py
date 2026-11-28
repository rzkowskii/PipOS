"""The source-id: one hash for the files that go into a PipOS image.

An image built from this tree carries the same id in /etc/being/source-id
(written by post-build.sh from BEING_SOURCE_ID) and, beside the image on the
Mac, in a sidecar file `IMAGE.source-id` (written by the build). Lab suites
compare the image's id with the tree's before they boot it, so a green result
can no longer come from an image built days earlier from other code.

    python3 -m lab.source_id                 # print the tree's id
    python3 -m lab.source_id check IMAGE     # compare an image with the tree
    python3 -m lab.source_id stamp IMAGE ID  # write IMAGE.source-id

Take ID *before* the build starts (and pass the same value to the builder as
BEING_SOURCE_ID): a build takes most of an hour, and the tree can change under
it. Stamping with the id of the tree as it is after the build could label an
image with code it never contained.

Only image inputs count (Rust crates, the mind, config, the OS tree, the
simulator, the staged model and the few lab files the build reads: see
candidate_manifest.OPTIONAL_IMAGE_SCOPES). Editing a lab test or a document
does not make an image stale. BEING_ALLOW_STALE=1 lets a suite run on a stale
image; BEING_GATE=1 also refuses an image whose id is unknown.
BEING_IMAGE_SOURCE_ID=ID states the id of an image that has no sidecar (the
frozen first-boot stick, for example); it is reported on every run.
"""

import functools
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from lab import candidate_manifest

ROOT = Path(__file__).resolve().parent.parent
IMAGE_SCOPES = candidate_manifest.IMAGE_SCOPES
OPTIONAL_IMAGE_SCOPES = candidate_manifest.OPTIONAL_IMAGE_SCOPES
SIDECAR = ".source-id"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class StaleImage(RuntimeError):
    pass


def tree_files(root=ROOT):
    return candidate_manifest.image_source_files(root)


def tree_id(root=ROOT):
    return candidate_manifest.image_source_id(root)


@functools.lru_cache(maxsize=4)
def cached_tree_id(root=ROOT):
    return tree_id(root)


def sidecar(image):
    image = Path(image)
    return image.with_name(image.name + SIDECAR)


def _stated():
    return bool(HEX64.match(os.environ.get("BEING_IMAGE_SOURCE_ID", "").strip().lower()))


def image_id(image):
    """The id recorded for an image, or None when nothing says what it was built from."""
    override = os.environ.get("BEING_IMAGE_SOURCE_ID", "").strip().lower()
    if HEX64.match(override):
        return override
    try:
        text = sidecar(image).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return text if HEX64.match(text) else None


def check(image, root=ROOT):
    """('match' | 'stale' | 'unknown', message)."""
    recorded = image_id(image)
    if recorded is None:
        return "unknown", f"{image}: no source-id recorded ({sidecar(Path(image)).name} missing)"
    current = cached_tree_id(Path(root))
    if recorded == current:
        stated = " (stated by BEING_IMAGE_SOURCE_ID, not read from a sidecar)" if _stated() else ""
        return "match", f"{image}: built from this tree ({current[:12]}){stated}"
    return "stale", (f"{image} was built from source {recorded[:12]}, but the tree is now {current[:12]}. "
                     "Rebuild it, or set BEING_ALLOW_STALE=1 to test it anyway.")


def enforce(image, root=ROOT, *, env=None, warn=None):
    """Refuse a stale image (and, in gate mode, an unidentified one)."""
    env = os.environ if env is None else env
    warn = warn or (lambda message: print(f"source-id: {message}", file=sys.stderr, flush=True))
    status, message = check(image, root)
    allow_stale = env.get("BEING_ALLOW_STALE") == "1"
    gate = env.get("BEING_GATE") == "1"
    if status == "stale" and not allow_stale:
        raise StaleImage(message)
    if status == "unknown" and gate and not allow_stale:
        raise StaleImage(message + "; BEING_GATE=1 needs images with a recorded source-id")
    if status != "match" or _stated():
        warn(message)
    return status


def stamp(image, root=ROOT, ident=None):
    """Write IMAGE.source-id: `ident` (taken before the build), else the tree's id now."""
    ident = (ident or tree_id(root)).strip().lower()
    if not HEX64.match(ident):
        raise ValueError(f"not a source-id: {ident!r}")
    path = sidecar(image)
    path.write_text(ident + "\n", encoding="utf-8")
    return path


def stamp_verified_install(try_image, target, *, role="try_image"):
    """Bind a disposable installed disk to Try after checking both system slots."""
    roots = []
    for path, layout in ((try_image, role), (target, "installed_image")):
        path = Path(path)
        with path.open("rb") as stream:
            record = candidate_manifest.disk_record(stream, path.stat().st_size, layout)
        roots.append([part["rootfs"] for part in record["partitions"] if "rootfs" in part])
    if roots[1] != roots[0] * 2:
        raise ValueError("The installed system slots do not both match the Try USB.")
    ident = image_id(try_image)
    if ident is not None:
        stamp(target, ident=ident)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(tree_id())
        return 0
    if len(argv) == 3 and argv[0] == "stamp":
        print(stamp(argv[1], ident=argv[2]))
        return 0
    if len(argv) == 2 and argv[0] == "check":
        status, message = check(argv[1])
        print(message)
        return 0 if status == "match" else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

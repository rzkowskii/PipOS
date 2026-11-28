"""Prepare an unsigned Alba retrain input for the existing image packager."""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from lab.candidate_manifest import fingerprint, regular
from lab.hardware_image import ALBA_REQUIRED_ESPEAK_DATA as REQUIRED_DATA
from lab.hardware_image import ALBA_RETRAIN_FILES, ALBA_RETRAIN_REVISION
from lab.package_model import REPO, produce, render_descriptor
from mind.model_pack import FORMAT_V2, MAX_DESCRIPTOR, ROLE_DIRS, check_descriptor

ARCHIVE = "tts/piper-alba-retrain/alba-retrain.tar.bz2"
NOTICE = REPO / "research/alba-public/NOTICES.md"
VOICE_LICENSE = "MIT declaration (voice); CC BY 4.0 (Alba and Hi-Fi TTS); GPL-3.0-or-later (eSpeak NG data)"
ESPEAK_REVISION = "ed530aa113046142eb5115cf2fc9157854d0ffe1"
ESPEAK_SOURCE = {"revision": ESPEAK_REVISION, "bytes": 18011501,
                 "sha256": "e4e262cbe34f7fe21f91f1ba3397f2728e1f30eafbae7853f2b753a9ed13f0dd"}
CMAKE_FLAGS = (
    "-DBUILD_ESPEAK_NG_EXE=ON", "-DBUILD_ESPEAK_NG_TESTS=OFF", "-DBUILD_TESTING=OFF",
    "-DBUILD_SHARED_LIBS=OFF", "-DUSE_ASYNC=OFF", "-DUSE_MBROLA=OFF", "-DUSE_LIBSONIC=OFF",
    "-DUSE_LIBPCAUDIO=OFF", "-DUSE_KLATT=OFF", "-DUSE_SPEECHPLAYER=OFF",
    "-DEXTRA_cmn=ON", "-DEXTRA_ru=ON",
)


def build_espeak_data(source, scratch):
    """Generate the pronunciation data from the exact source Piper pins."""
    with regular(source) as (stream, info):
        if info.st_size != ESPEAK_SOURCE["bytes"]:
            raise ValueError("The eSpeak NG archive does not match the pinned source size.")
        content = stream.read(info.st_size + 1)
    if hashlib.sha256(content).hexdigest() != ESPEAK_SOURCE["sha256"]:
        raise ValueError("The eSpeak NG archive does not match the pinned source digest.")
    cmake = shutil.which("cmake")
    if cmake is None:
        raise ValueError("Install CMake and a native C compiler to generate the eSpeak NG data.")
    # The bytes in memory are the ones just checked, independent of later
    # changes to the supplied archive. CMake's data target runs host tools.
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        archive.extractall(scratch)
    source_tree = scratch / f"espeak-ng-{ESPEAK_REVISION}"
    build = scratch / "build"
    environment = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    environment.pop("VALGRIND", None)
    from lab.candidate_tests import batch

    steps = (
        ("configure", [cmake, "-S", str(source_tree), "-B", str(build), *CMAKE_FLAGS], 180),
        ("data", [cmake, "--build", str(build), "--target", "data", "--parallel", "4"], 900),
    )
    for name, command, timeout in steps:
        log = scratch / f"espeak-{name}.log"
        result = batch(command, environment, log, timeout)
        if result != "passed":
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 4096))
                detail = stream.read().decode("utf-8", errors="replace").strip()
            raise ValueError(f"eSpeak NG {name} {result}.\n{detail}")
    return build / "espeak-ng-data"


def _entries(prepared, data):
    if data.is_symlink() or not data.is_dir():
        raise ValueError("Pass the generated eSpeak NG data directory.")
    assets = {name: prepared / name for name in ALBA_RETRAIN_FILES}
    for path in sorted(data.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Linked eSpeak data file: {path.relative_to(data)}.")
        if not path.is_dir():
            assets[f"espeak-ng-data/{path.relative_to(data).as_posix()}"] = path
    required = (f"espeak-ng-data/{name}" for name in sorted(REQUIRED_DATA))
    missing = [name for name in required if name not in assets or assets[name].stat().st_size <= 0]
    if missing:
        raise ValueError("Missing compiled eSpeak NG data: " + ", ".join(missing) + ".")
    return assets


def _archive(archive_path, prepared, data):
    assets = _entries(prepared, data)
    notice = NOTICE.read_bytes()
    with tarfile.open(archive_path, "w:bz2", format=tarfile.USTAR_FORMAT) as bundle:
        for name, path in sorted(assets.items()):
            with regular(path) as (stream, info):
                if name in ALBA_RETRAIN_FILES:
                    expected_size, expected_sha = ALBA_RETRAIN_FILES[name]
                    if info.st_size != expected_size or fingerprint(stream, expected_size)["sha256"] != expected_sha:
                        raise ValueError(f"Prepared Alba file differs from the pinned retrain: {name}.")
                    stream.seek(0)
                entry = tarfile.TarInfo(name)
                entry.size = info.st_size
                bundle.addfile(entry, stream)
        entry = tarfile.TarInfo(NOTICE.name)
        entry.size = len(notice)
        bundle.addfile(entry, io.BytesIO(notice))


def stage(prepared, data, output, *, espeak_source=None):
    """Write a new local source tree and descriptor. Signing remains with Buildroot."""
    if output.exists() or output.is_symlink():
        raise ValueError("Choose a new Alba staging directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".alba-retrain-", dir=output.parent) as folder:
        tree = Path(folder) / "staged"
        archive = tree / "voice" / ARCHIVE
        archive.parent.mkdir(parents=True)
        _archive(archive, prepared, data)
        with archive.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        size = archive.stat().st_size
        extracts = [{"member": name, "path": name} for name in (*ALBA_RETRAIN_FILES, NOTICE.name)]
        extracts.append({"prefix": "espeak-ng-data/", "path": "espeak-ng-data/"})
        source = {"file": f"voice/{ARCHIVE}", "bytes": size, "sha256": sha256, "extract": extracts}
        download = {"name": ARCHIVE, "size_bytes": size, "sha256": sha256, "licence": VOICE_LICENSE,
                    "version": ALBA_RETRAIN_REVISION, "notes": "Locally prepared Alba and compiled eSpeak NG data."}
        (tree / "voice/DOWNLOADS.json").write_text(json.dumps({"files": [download]}, indent=2) + "\n",
                                                   encoding="utf-8")
        pack = {"format": FORMAT_V2, "role": "tts", "dir": ROLE_DIRS["tts"],
                "model": "Piper en_GB Alba medium retrain (Sherpa VITS)", "license": VOICE_LICENSE,
                "revision": ALBA_RETRAIN_REVISION, "sources": [source]}
        if espeak_source is not None:
            pack["espeak_source"] = espeak_source
        pack["files"] = [{"path": name, "bytes": count, "sha256": digest}
                         for name, (count, digest) in sorted(produce(tree, pack).items())]
        descriptor = render_descriptor(check_descriptor(pack, "tts"))
        if len(descriptor) > MAX_DESCRIPTOR:
            raise ValueError("The Alba voice pack descriptor exceeds the runtime limit.")
        (tree / "pack.json").write_bytes(descriptor)
        tree.rename(output)
    return pack


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", required=True, type=Path, help="prepared pinned Alba retrain files")
    espeak = parser.add_mutually_exclusive_group(required=True)
    espeak.add_argument("--espeak-source", type=Path, help="build from the pinned eSpeak NG source ZIP")
    espeak.add_argument("--espeak-data", type=Path, help="use already compiled pronunciation data")
    parser.add_argument("--output", required=True, type=Path, help="new directory for the archive and descriptor")
    args = parser.parse_args(argv)
    try:
        if args.espeak_source is None:
            pack = stage(args.prepared, args.espeak_data, args.output)
        else:
            if args.output.exists() or args.output.is_symlink():
                raise ValueError("Choose a new Alba staging directory.")
            with tempfile.TemporaryDirectory(prefix=".alba-espeak-") as folder:
                data = build_espeak_data(args.espeak_source, Path(folder))
                pack = stage(args.prepared, data, args.output, espeak_source=ESPEAK_SOURCE)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Could not prepare Alba pack: {error}\n")
    print(f"Prepared {len(pack['files'])} voice files in {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fingerprint the built files before Buildroot assembles the root filesystem."""

import hashlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path


def stamp_source_id(root, source_id):
    output = root / "etc/being/source-id"
    if not source_id:
        output.unlink(missing_ok=True)
    elif re.fullmatch(r"[0-9a-f]{64}", source_id):
        output.write_text(source_id + "\n", encoding="ascii")
    else:
        raise SystemExit("post-build: invalid BEING_SOURCE_ID")


def install_speech_verdict(root, verdict):
    from mind.voice_engines import SMOKE_VERDICTS, sherpa_smoke_passed

    output = root / SMOKE_VERDICTS[0].relative_to("/")
    output.unlink(missing_ok=True)
    library = root / "usr/lib/libsherpa-onnx-c-api.so"
    if not verdict.is_file() or not library.is_file():
        return
    passed, reason = sherpa_smoke_passed(paths=(verdict,), library=library)
    if passed:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(verdict, output)
    else:
        print(f"post-build: Ivy Bridge speech verdict was not installed ({reason})", file=sys.stderr)


def build_id(root):
    digest = hashlib.sha256()
    output = root / 'etc/being/build-id'
    for directory, subdirectories, files in os.walk(root, topdown=True):
        subdirectories.sort()
        for name in sorted((*subdirectories, *files)):
            path = Path(directory) / name
            if path == output:
                continue
            mode = path.lstat().st_mode
            digest.update(os.fsencode(path.relative_to(root)) + b'\0')
            digest.update(f'{mode:o}\0'.encode('ascii'))
            if stat.S_ISLNK(mode):
                digest.update(os.fsencode(os.readlink(path)) + b'\0')
            elif stat.S_ISREG(mode):
                with path.open('rb') as stream:
                    digest.update(hashlib.file_digest(stream, 'sha256').digest())
    output.write_text(digest.hexdigest() + '\n', encoding='ascii')


if __name__ == '__main__':
    target = Path(sys.argv[1])
    board_path = Path(__file__).resolve()
    sys.path.insert(0, str(board_path.parents[3]))
    verdict = board_path.parents[2] / "package/sherpa-onnx/ivybridge-smoke.json"
    stamp_source_id(target, os.environ.get("BEING_SOURCE_ID"))
    install_speech_verdict(target, verdict)
    build_id(target)

"""Fingerprint the built files before Buildroot assembles the root filesystem."""

import hashlib
import os
import stat
import sys
from pathlib import Path


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
    build_id(Path(sys.argv[1]))

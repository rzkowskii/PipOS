"""The ctypes structs in mind/sherpa.py match the C header of each pinned release.

lab/sherpa_layout_check.c is compiled on the host against the c-api.h inside
each downloaded release archive (out/voice/sherpa-onnx/v2.0.x) and its sizes
and offsets are compared with ctypes. The test skips when no C compiler or no
archive is present (a fresh clone has no downloads).
"""

import ctypes
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from mind import sherpa

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "lab/sherpa_layout_check.c"
RELEASES = {version: ROOT / f"out/voice/sherpa-onnx/v{version}/sherpa-onnx-v{version}-linux-x64-shared.tar.bz2"
            for version in sherpa.CHECKED_VERSIONS}
HEADER = "include/sherpa-onnx/c-api/c-api.h"


def c_layout(archive, folder):
    prefix = archive.name.removesuffix(".tar.bz2")
    with tarfile.open(archive, "r:bz2") as bundle:
        member = bundle.getmember(f"{prefix}/{HEADER}")
        if not member.isfile():
            raise ValueError("the header is not a regular file")
        target = Path(folder) / HEADER
        target.parent.mkdir(parents=True)
        with bundle.extractfile(member) as source:
            target.write_bytes(source.read())
    binary = Path(folder) / "layout"
    subprocess.run([shutil.which("cc"), "-std=c11", "-Wall", "-Werror", "-Wno-deprecated-declarations",
                    "-I", str(Path(folder) / "include"), "-o", str(binary), str(CHECK)],
                   check=True, capture_output=True, timeout=120)
    output = subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=30).stdout
    sizes, offsets = {}, {}
    for line in output.splitlines():
        kind, name, number = line.split()
        (sizes if kind == "struct" else offsets)[name] = int(number)
    return sizes, offsets


class LayoutTests(unittest.TestCase):
    def test_check_program_names_every_mirrored_member(self):
        source = CHECK.read_text()
        for name, struct in sherpa.STRUCTS.items():
            self.assertIn(f"S({name});", source)
            for field, _ in struct._fields_:
                self.assertIn(f"F({name}, {field});", source)

    def test_ctypes_matches_each_release_header(self):
        if shutil.which("cc") is None:
            self.skipTest("no C compiler on this host")
        found = {version: path for version, path in RELEASES.items() if path.is_file()}
        if not found:
            self.skipTest("no sherpa-onnx release archive downloaded")
        for version, archive in found.items():
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                sizes, offsets = c_layout(archive, folder)
                self.assertEqual(set(sizes), set(sherpa.STRUCTS))
                for name, struct in sherpa.STRUCTS.items():
                    self.assertEqual(ctypes.sizeof(struct), sizes[name], f"sizeof {name}")
                    for field, _ in struct._fields_:
                        self.assertEqual(getattr(struct, field).offset, offsets[f"{name}.{field}"],
                                         f"offsetof {name}.{field}")


if __name__ == "__main__":
    unittest.main()

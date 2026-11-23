"""The Buildroot package for the prebuilt sherpa-onnx release (os/package/sherpa-onnx)."""

import hashlib
import json
import re
import tarfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "os/package/sherpa-onnx"
DOWNLOADS = ROOT / "out/voice/DOWNLOADS.json"


def hashes():
    entries = {}
    for line in (PACKAGE / "sherpa-onnx.hash").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            kind, digest, name = line.split()
            assert kind == "sha256"
            entries[name] = digest
    return entries


def make_value(name):
    text = (PACKAGE / "sherpa-onnx.mk").read_text().replace("\\\n", " ")
    return re.search(rf"^{name}\s*=\s*(.*)$", text, re.M)[1].split()


class PackageTests(unittest.TestCase):
    def test_both_releases_are_pinned_and_v2_0_3_is_the_default(self):
        entries = hashes()
        for version in ("2.0.3", "2.0.2"):
            self.assertRegex(entries[f"sherpa-onnx-v{version}-linux-x64-shared.tar.bz2"], r"^[0-9a-f]{64}$")
        config = (PACKAGE / "Config.in").read_text()
        self.assertIn("default BR2_PACKAGE_SHERPA_ONNX_V2_0_3", config)
        self.assertIn("config BR2_PACKAGE_SHERPA_ONNX_V2_0_2", config)
        mk = (PACKAGE / "sherpa-onnx.mk").read_text()
        self.assertIn("SHERPA_ONNX_VERSION = 2.0.3", mk)
        self.assertIn("SHERPA_ONNX_VERSION = 2.0.2", mk)
        self.assertIn("SHERPA_ONNX_SOURCE = sherpa-onnx-v$(SHERPA_ONNX_VERSION)-linux-x64-shared.tar.bz2", mk)

    def test_pins_match_the_recorded_downloads(self):
        if not DOWNLOADS.is_file():
            self.skipTest("no download record in this checkout")
        recorded = {Path(entry["name"]).name: entry["sha256"]
                    for entry in json.loads(DOWNLOADS.read_text())["files"]}
        for name, digest in hashes().items():
            if name.endswith(".tar.bz2"):
                self.assertEqual(recorded[name], digest, name)

    def test_licence_files_exist_and_their_hashes_are_current(self):
        entries = hashes()
        files = make_value("SHERPA_ONNX_LICENSE_FILES")
        self.assertEqual(set(files), {"LICENSE-sherpa-onnx-Apache-2.0", "LICENSE-onnxruntime-MIT",
                                      "COPYING-espeak-ng-GPL-3.0", "LICENSES.md"})
        for name in files:
            self.assertEqual(hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest(), entries[name], name)
        self.assertIn("Apache License", (PACKAGE / "LICENSE-sherpa-onnx-Apache-2.0").read_text())
        self.assertIn("Microsoft Corporation", (PACKAGE / "LICENSE-onnxruntime-MIT").read_text())
        self.assertIn("Version 3, 29 June 2007", (PACKAGE / "COPYING-espeak-ng-GPL-3.0").read_text())
        licence = " ".join(make_value("SHERPA_ONNX_LICENSE"))
        for name in ("Apache-2.0", "MIT", "GPL-3.0+"):
            self.assertIn(name, licence)

    def test_credits_name_alba_and_espeak(self):
        text = " ".join((PACKAGE / "LICENSES.md").read_text().split())
        for phrase in ("CC BY 4.0", "Centre for Speech Technology Research", "University of Edinburgh",
                       "https://datashare.ed.ac.uk/handle/10283/3270", "GPL-3.0-or-later", "1.28.2",
                       "Silero", "Moonshine"):
            self.assertIn(phrase, text)

    def test_installs_only_the_two_libraries_and_the_licences(self):
        mk = (PACKAGE / "sherpa-onnx.mk").read_text()
        for library in ("libsherpa-onnx-c-api.so", "libonnxruntime.so"):
            self.assertIn(f"$(@D)/lib/{library} $(TARGET_DIR)/usr/lib/{library}", mk)
        self.assertIn("$(TARGET_DIR)/usr/share/licenses/sherpa-onnx/$(text)", mk)
        self.assertNotIn("/usr/bin", mk)
        self.assertIn("$(eval $(generic-package))", mk)

    def test_the_mind_loads_the_installed_path(self):
        from mind import sherpa

        self.assertEqual(sherpa.LIBRARY, Path("/usr/lib/libsherpa-onnx-c-api.so"))

    def test_the_pinned_archive_holds_both_libraries(self):
        archive = ROOT / "out/voice/sherpa-onnx/v2.0.3/sherpa-onnx-v2.0.3-linux-x64-shared.tar.bz2"
        if not archive.is_file():
            self.skipTest("the release archive is not downloaded")
        with tarfile.open(archive, "r:bz2") as bundle:
            names = set(bundle.getnames())
        for library in ("libsherpa-onnx-c-api.so", "libonnxruntime.so"):
            self.assertIn(f"sherpa-onnx-v2.0.3-linux-x64-shared/lib/{library}", names)


if __name__ == "__main__":
    unittest.main()

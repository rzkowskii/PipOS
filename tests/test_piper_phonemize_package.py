"""The Buildroot package for the prebuilt piper-phonemize release (os/package/piper-phonemize)."""

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "os/package/piper-phonemize"
ARCHIVE = "piper-phonemize-v1.5.9-linux-x64-shared-lib.tar.bz2"


def hashes():
    entries = {}
    for line in (PACKAGE / "piper-phonemize.hash").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            kind, digest, name = line.split()
            assert kind == "sha256"
            entries[name] = digest
    return entries


def make_value(name):
    text = (PACKAGE / "piper-phonemize.mk").read_text().replace("\\\n", " ")
    return re.search(rf"^{name}\s*=\s*(.*)$", text, re.M)[1].split()


class PackageTests(unittest.TestCase):
    def test_the_release_is_pinned_by_its_published_digest(self):
        self.assertEqual(hashes()[ARCHIVE],
                         "609569420d356f561e13ee6d52ed0edf7dcae225ea498f4cc68587c11d6d295c")
        mk = (PACKAGE / "piper-phonemize.mk").read_text()
        self.assertIn("PIPER_PHONEMIZE_VERSION = 1.5.9", mk)
        self.assertIn("PIPER_PHONEMIZE_SOURCE = piper-phonemize-v$(PIPER_PHONEMIZE_VERSION)-linux-x64-shared-lib.tar.bz2",
                      mk)
        # The archive has lib/ and include/ at its top: nothing to strip.
        self.assertIn("PIPER_PHONEMIZE_STRIP_COMPONENTS = 0", mk)

    def test_the_bindings_check_the_same_release(self):
        from mind import phonemize
        self.assertEqual(phonemize.CHECKED_VERSIONS, ("1.5.9",))
        self.assertEqual(phonemize.LIBRARY, Path("/usr/lib/libpiper_phonemize_core.so"))

    def test_installs_only_the_library_and_the_licences(self):
        mk = (PACKAGE / "piper-phonemize.mk").read_text()
        self.assertIn("$(@D)/lib/libpiper_phonemize_core.so $(TARGET_DIR)/usr/lib/libpiper_phonemize_core.so", mk)
        self.assertIn("$(TARGET_DIR)/usr/share/licenses/piper-phonemize/$(text)", mk)
        install = mk.split("define PIPER_PHONEMIZE_INSTALL_TARGET_CMDS")[1]
        for absent in ("$(TARGET_DIR)/usr/bin", "$(TARGET_DIR)/usr/include", "espeak-ng-data", "cxx_api"):
            self.assertNotIn(absent, install)
        self.assertIn("$(eval $(generic-package))", mk)

    def test_licence_files_exist_and_their_hashes_are_current(self):
        entries = hashes()
        files = make_value("PIPER_PHONEMIZE_LICENSE_FILES")
        self.assertEqual(set(files), {"COPYING-GPL-3.0", "LICENSE-uni-algo", "COPYING-espeak-ng-UCD", "LICENSES.md"})
        for name in files:
            self.assertEqual(hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest(), entries[name], name)
        self.assertIn("Version 3, 29 June 2007", (PACKAGE / "COPYING-GPL-3.0").read_text())
        self.assertIn("MIT License", (PACKAGE / "LICENSE-uni-algo").read_text())
        self.assertIn("Unicode Data Files", (PACKAGE / "COPYING-espeak-ng-UCD").read_text())
        licence = " ".join(make_value("PIPER_PHONEMIZE_LICENSE"))
        self.assertIn("GPL-3.0+", licence)

    def test_the_notice_names_the_corresponding_source(self):
        text = " ".join((PACKAGE / "LICENSES.md").read_text().split())
        for phrase in ("GPL-3.0-or-later", "v1.5.9", "ed530aa113046142eb5115cf2fc9157854d0ffe1",
                       "e4e262cbe34f7fe21f91f1ba3397f2728e1f30eafbae7853f2b753a9ed13f0dd",
                       "build-linux-x64-shared", "Corresponding source"):
            self.assertIn(phrase, text)

    def test_it_comes_with_the_speech_library(self):
        config = (PACKAGE / "Config.in").read_text()
        self.assertIn("config BR2_PACKAGE_PIPER_PHONEMIZE", config)
        self.assertIn("default y if BR2_PACKAGE_SHERPA_ONNX", config)
        for name in ("beingos_laptop_defconfig", "beingos_x86_64_defconfig"):
            defconfig = (ROOT / "os/configs" / name).read_text()
            self.assertIn("BR2_PACKAGE_SHERPA_ONNX=y", defconfig, name)
            self.assertNotIn("# BR2_PACKAGE_PIPER_PHONEMIZE is not set", defconfig, name)


if __name__ == "__main__":
    unittest.main()

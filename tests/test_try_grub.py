"""The Try boot menu source, as the lab and the laptop media receive it.

lab/tests/test_laptop_smoke.py checks the built stick; this checks
os/board/beingos/try-grub.cfg itself without a build, including the shape
GRUB's parser needs (grub-script-check runs only in the builder).
"""

import os
import re
import shlex
import subprocess
import unittest
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "os/board/beingos/try-grub.cfg"
# Each extra laptop entry is the default entry plus these kernel arguments.
EXTRA_ARGS = {
    "Try PipOS (alternative microphone)": ["snd_hda_intel.model=mbp81", "being.mic_channel=auto",
                                          "being.mic_test=1"],
    "Try PipOS (alternative microphone 2)": ["snd_hda_intel.model=mbp101", "being.mic_test=1"],
    "Try PipOS (measure speed)": ["being.bench=1"],
    "Try PipOS (measure boot and idle)": ["being.bench=2"],
}


SLOTS = CONFIG.parent / "slots.sh"


def boot_console(config, variant):
    """The file as boot_console() in slots.sh passes it on for that variant."""
    result = subprocess.run(["/bin/sh", "-c", '. "$1" && boot_console', "render", str(SLOTS)],
                            input=config, capture_output=True, text=True, check=True,
                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "BEINGOS_VARIANT": variant})
    return result.stdout


def laptop(config):
    """What boot_console() in slots.sh leaves for BEINGOS_VARIANT=laptop."""
    return boot_console(config, "laptop")


def entries(config):
    """[(title, options, body)] for each top-level menuentry block."""
    return [(match[1], match[2].split(), match[3]) for match in re.finditer(
        r'^ *menuentry +"([^"]+)"([^{\n]*)\{\n(.*?)^ *\}$', config, re.M | re.S)]


def kernel_args(body):
    match = re.search(r'^\s*if linux "\(\$disk,gpt2\)/boot/bzImage" (.*); then$', body, re.M)
    return match[1].split() if match else None


def words(line):
    """The words GRUB's lexer would see, quotes balanced (shlex is close enough here)."""
    return shlex.split(line, comments=True)


class TryGrubSource(unittest.TestCase):
    def setUp(self):
        self.config = CONFIG.read_text(encoding="utf-8")

    def test_the_lab_entry_is_one_line_with_its_serial_console(self):
        lab = [line for line in self.config.splitlines() if line.startswith("serial --unit=")]
        self.assertEqual(len(lab), 1)
        self.assertEqual(lab[0].count("menuentry"), 1)
        self.assertIn("console=ttyS0,115200", lab[0])
        self.assertNotIn("mic_channel", lab[0])
        self.assertIn("terminal_output serial", self.config.splitlines())

    def test_the_laptop_gets_seven_entries_in_order_with_their_hotkeys(self):
        found = entries(laptop(self.config))
        titles = [title for title, _, _ in found]
        self.assertEqual(titles, ["Try PipOS", "Try PipOS (show boot messages)", "Try PipOS (safe graphics)",
                                  "Try PipOS (alternative microphone)", "Try PipOS (alternative microphone 2)",
                                  "Try PipOS (measure speed)", "Try PipOS (measure boot and idle)"])
        self.assertEqual([options for _, options, _ in found],
                         [[], ["--hotkey=v"], ["--hotkey=s"], ["--hotkey=m"], ["--hotkey=n"],
                          ["--hotkey=b"], ["--hotkey=i"]])
        self.assertNotIn("ttyS0", laptop(self.config))
        self.assertNotIn("serial", "\n".join(line for line in laptop(self.config).splitlines()
                                             if not line.lstrip().startswith("#")))

    def test_the_lab_variant_passes_the_file_through_unchanged(self):
        lab = boot_console(self.config, "lab")
        # Only the laptop's argument tokens go.
        self.assertEqual(lab, re.sub(r" BEING_[A-Z]+_ARGS", "", self.config))
        # GRUB sets being_lab on the first line, so the laptop block is skipped there.
        first = next(line for line in lab.splitlines() if line and not line.startswith("#"))
        self.assertTrue(first.startswith("serial --unit=0 --speed=115200; set being_lab=1; menuentry"), first)
        self.assertNotIn("being_lab=1", laptop(self.config))

    def test_each_extra_entry_is_the_default_plus_its_own_arguments(self):
        found = {title: body for title, _, body in entries(laptop(self.config))}
        default = kernel_args(found["Try PipOS"])
        self.assertIsNotNone(default)
        self.assertIn("hid_apple.fnmode=1", default)
        for title, extra in EXTRA_ARGS.items():
            with self.subTest(title=title):
                self.assertEqual(kernel_args(found[title]), default + extra)
        for title, body in found.items():
            if title not in EXTRA_ARGS:
                for word in ("mic_channel", "snd_hda_intel", "being.bench"):
                    self.assertNotIn(word, body, title)

    def test_every_line_parses_into_balanced_words_and_blocks(self):
        depth, stack = 0, []
        for number, line in enumerate(self.config.splitlines(), 1):
            for word in (word.rstrip(";") for word in words(line)):  # words() raises on an unbalanced quote
                if word == "{":
                    depth += 1
                elif word == "}":
                    depth -= 1
                    self.assertGreaterEqual(depth, 0, f"line {number}: a block closes that never opened")
                elif word == "if":
                    stack.append("if")
                elif word == "then":
                    self.assertEqual(stack[-1:], ["if"], f"line {number}: then without if")
                    stack[-1] = "then"
                elif word == "else":
                    self.assertEqual(stack[-1:], ["then"], f"line {number}: else outside an if")
                    stack[-1] = "else"
                elif word == "fi":
                    self.assertIn(stack[-1:], (["then"], ["else"]), f"line {number}: fi without then")
                    stack.pop()
        self.assertEqual(depth, 0, "unclosed block")
        self.assertEqual(stack, [], "unclosed if")

    def test_hotkeys_do_not_collide_with_grub_keys(self):
        hotkeys = re.findall(r"--hotkey=(\S+)", self.config)
        self.assertEqual(len(hotkeys), len(set(hotkeys)))
        # e edits an entry and c opens GRUB's command line.
        for key in hotkeys:
            self.assertRegex(key, r"^[a-z]$")
            self.assertNotIn(key, ("e", "c"))


if __name__ == "__main__":
    unittest.main()

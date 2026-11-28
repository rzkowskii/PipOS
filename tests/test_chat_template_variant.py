"""config/chat-template-nothink.jinja: the candidate without the empty thinking block (not installed).

Host test of the text only: jinja2 is not on the host, so nothing is rendered. What
decides it is on the laptop: `prompt_n` on a warm turn equal to the new user block, and
the model audition (docs/manual/ARCHITECTURE.md, the prompt diet).
"""

import difflib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLED = ROOT / "config/chat-template.jinja"
VARIANT = ROOT / "config/chat-template-nothink.jinja"


def body(path):
    """The template without its leading comment."""
    text = path.read_text()
    return text[text.index("{%- set image_count"):]


class Variant(unittest.TestCase):
    def changes(self):
        old, new = body(INSTALLED).splitlines(), body(VARIANT).splitlines()
        return [line for line in difflib.unified_diff(old, new, lineterm="", n=0) if line[:1] in "+-" and line[:3] not in ("+++", "---")]

    def test_it_differs_in_exactly_the_two_thinking_block_places(self):
        changes = self.changes()
        self.assertEqual(len(changes), 4, changes)
        removed = [line for line in changes if line.startswith("-")]
        added = [line for line in changes if line.startswith("+")]
        self.assertTrue(any("</think>" in line and "message.role" in line for line in removed))
        self.assertTrue(any("'<|im_start|>' + message.role + '\\n' + content" in line for line in added))
        self.assertTrue(any("<think>\\n\\n</think>\\n\\n" in line for line in removed))
        # Thinking on still opens a block.
        self.assertIn("{{- '<think>\\n' }}", body(VARIANT))

    def test_the_variant_renders_no_empty_block_anywhere(self):
        text = body(VARIANT)
        self.assertNotIn("</think>\\n\\n' + content", text)
        self.assertNotIn("<think>\\n\\n</think>", text)

    def test_the_installed_template_and_the_unit_are_untouched(self):
        self.assertIn("<think>\\n\\n</think>\\n\\n", body(INSTALLED))
        unit = (ROOT / "os/package/cortex/being-cortex.service").read_text()
        self.assertIn("/usr/share/being/chat-template.jinja", unit)
        self.assertNotIn("nothink", unit)


if __name__ == "__main__":
    unittest.main()

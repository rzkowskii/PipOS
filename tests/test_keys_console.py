"""being-console's top-row keys (the Try menu and setup screen).

The laptop boots with hid_apple.fnmode=1, so the MacBookPro9,2's top row sends
what is printed on it. On the console a level key leaves a request in the
screen gate (as F9 and F10 do) for mind/local_screen.py to send on; no media
key is ever typed into the session. The functions are compiled on their own
against the host SDL2 headers, and the whole console is built as
tests/test_console_panel.py does.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "os/package/being/being-console.c"
DRAW = ROOT / "os/package/being/being-console-draw.h"
PANEL = ROOT / "os/package/being/being-console-panel.h"
SDL2_CONFIG = shutil.which("sdl2-config") or next(
    (path for path in ("/opt/homebrew/bin/sdl2-config", "/usr/local/bin/sdl2-config") if Path(path).exists()), None)
COMPILER = shutil.which("cc")

# What the face sends for the same keys (crates/face/src/keys.rs); the gate
# names are what mind/local_screen.py reads.
EXPECTED = {
    "SDL_SCANCODE_BRIGHTNESSDOWN": "brightness-down",
    "SDL_SCANCODE_BRIGHTNESSUP": "brightness-up",
    "SDL_SCANCODE_KBDILLUMDOWN": "kbd-light-down",
    "SDL_SCANCODE_KBDILLUMUP": "kbd-light-up",
    "SDL_SCANCODE_KBDILLUMTOGGLE": "kbd-light-toggle",
    "SDL_SCANCODE_MUTE": "speaker-mute",
    "SDL_SCANCODE_AUDIOMUTE": "speaker-mute",
    "SDL_SCANCODE_VOLUMEDOWN": "volume-down",
    "SDL_SCANCODE_VOLUMEUP": "volume-up",
    "SDL_SCANCODE_AUDIOPLAY": "",
    "SDL_SCANCODE_AUDIOPREV": "",
    "SDL_SCANCODE_AUDIONEXT": "",
    "SDL_SCANCODE_EJECT": "",
    "SDL_SCANCODE_F1": None,
    "SDL_SCANCODE_F2": None,
    "SDL_SCANCODE_F9": None,
    "SDL_SCANCODE_F10": None,
    "SDL_SCANCODE_A": None,
    "SDL_SCANCODE_RETURN": None,
}

HARNESS = r"""
#include <SDL.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

%s

%s

static void show(const char *name, SDL_Scancode code)
{
    const char *request = media_request(code);
    printf("%%s %%s %%d\n", name, request ? (*request ? request : "-") : "none",
           request && *request ? (int)media_repeats(request) : 0);
}

int main(void)
{
%s
    return 0;
}
"""


def function(source, signature):
    match = re.search(rf"^static {re.escape(signature)}\n\{{\n.*?^\}}\n", source, re.M | re.S)
    return match[0] if match else None


class ConsoleSource(unittest.TestCase):
    def test_a_verbose_boot_ends_on_mission_control_and_names_keys_that_work(self):
        source = SOURCE.read_text(encoding="utf-8")
        hold = source[source.index("static void hold_for_boot_messages(void)"):]
        hold = hold[:hold.index("\n}\n")]
        self.assertIn("code == KEY_SCALE", hold, "the laptop's printed F2 is brightness up")
        self.assertNotIn("Esc or F2", hold)
        self.assertIn("press Esc or Enter to go on", hold)

    def test_a_media_key_is_handled_before_anything_can_type_it(self):
        source = SOURCE.read_text(encoding="utf-8")
        handle = function(source, "bool handle_key(SDL_KeyboardEvent event, struct session *session, "
                                  "struct terminal *terminal,\n                       const struct layout *geometry, "
                                  "bool visible, bool kms, int gate)")
        self.assertIsNotNone(handle, "handle_key() is not where this test looks for it")
        media = handle.index("media_request(key.scancode)")
        self.assertLess(media, handle.index("enqueue("), "a media key must never reach the session")
        self.assertLess(media, handle.index("SDLK_F9"))


FN_HARNESS = r"""
#include <dirent.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

%s

%s

int main(int argc, char **argv)
{
    const char *caps[2];
    for (int i = 1; i < argc; ++i) {
        bool fn = function_keys_need_fn_at(argv[i]);
        function_key_caps("F10", fn, caps);
        printf("%%d %%s %%s\n", (int)fn, caps[0], caps[1] ? caps[1] : "-");
    }
    return 0;
}
"""


class FooterKeys(unittest.TestCase):
    """With hid_apple.fnmode=1 the printed F10 is the speaker's mute and F9
    is next track, so a footer that says F10 Mic would mute the speaker and
    leave the microphone live. Every one of Pip's F-keys in a footer goes
    through function_key_caps(), which shows fn where it is needed."""

    def test_every_f_key_in_a_footer_names_fn_where_it_is_needed(self):
        for path, name in ((DRAW, "draw_footer"), (PANEL, "draw_panel_footer")):
            source = path.read_text(encoding="utf-8")
            start = source.index(f"static void {name}(")
            body = source[start:source.index("\n}\n", start)]
            rows = re.findall(r'\{ "([^"]+)", (?:NULL|"[^"]+"), "[^"]+" \}', body)
            self.assertTrue(rows, f"{name}: no footer items found")
            keys = [(index, key) for index, key in enumerate(rows) if re.fullmatch(r"F\d+", key)]
            self.assertTrue(keys, f"{name}: no F-keys in its footer")
            for index, key in keys:
                self.assertIn(f'function_key_caps("{key}", fn, items[{index}]);', body, f"{name}: {key}")

    def test_the_panel_redraws_its_footer_when_fn_changes(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertRegex(source, r"panel->control\.job_busy, scroll > 0, fn\);")
        self.assertIn("draw_panel_footer(canvas, geometry->face, geometry->pad_x, scroll > 0, fn);", source)

    @unittest.skipUnless(COMPILER, "needs a C compiler")
    def test_fn_is_named_only_for_an_apple_keyboard_with_a_printed_first_top_row(self):
        source = DRAW.read_text(encoding="utf-8")
        need = function(source, "bool function_keys_need_fn_at(const char *root)")
        caps = function(source, "void function_key_caps(const char *key, bool fn, const char *caps[2])")
        self.assertIsNotNone(need, "function_keys_need_fn_at() is not where this test looks for it")
        self.assertIsNotNone(caps, "function_key_caps() is not where this test looks for it")
        with tempfile.TemporaryDirectory(prefix="being-fn-keys-") as work:
            work = Path(work)

            def tree(name, fnmode, bound):
                root = work / name
                if fnmode is not None:
                    params = root / "module/hid_apple/parameters"
                    params.mkdir(parents=True)
                    (params / "fnmode").write_text(fnmode)
                driver = root / "bus/hid/drivers/apple"
                driver.mkdir(parents=True)
                for entry in ("bind", "unbind", "new_id", "uevent", "module"):
                    (driver / entry).write_text("")
                if bound:
                    (driver / "0003:05AC:0252.0001").mkdir()
                return str(root)

            roots = {
                "laptop": tree("laptop", "1\n", True),          # the MacBookPro9,2, fnmode=1
                "auto": tree("auto", "3\n", True),              # fnmode=3 is 1 for Apple's keyboards
                "fkeys": tree("fkeys", "2\n", True),            # round-2 sticks: F-keys first
                "off": tree("off", "0\n", True),
                "no-apple": tree("no-apple", "1\n", False),     # QEMU, a PC keyboard
                "no-module": tree("no-module", None, False),     # a Mac running the preview
                "junk": tree("junk", "12\n", True),
            }
            program, binary = work / "fn.c", work / "fn"
            program.write_text(FN_HARNESS % (need, caps))
            built = subprocess.run([COMPILER, "-std=c11", "-D_DEFAULT_SOURCE", "-D_DARWIN_C_SOURCE", "-Wall",
                                    "-Wextra", "-Werror", str(program), "-o", str(binary)],
                                   capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            output = subprocess.run([str(binary), *roots.values()], capture_output=True, text=True,
                                    check=True).stdout.split("\n")
        got = dict(zip(roots, output))
        self.assertEqual(got["laptop"], "1 fn F10", "the laptop's footer says fn F10 Mic")
        self.assertEqual(got["auto"], "1 fn F10")
        for name in ("fkeys", "off", "no-apple", "no-module", "junk"):
            self.assertEqual(got[name], "0 F10 -", name)


@unittest.skipUnless(SDL2_CONFIG and COMPILER, "needs a C compiler and the SDL2 headers (sdl2-config)")
class ConsoleMediaKeys(unittest.TestCase):
    def run_harness(self):
        source = SOURCE.read_text(encoding="utf-8")
        request = function(source, "const char *media_request(SDL_Scancode code)")
        repeats = function(source, "bool media_repeats(const char *request)")
        self.assertIsNotNone(request, "media_request() is not where this test looks for it")
        self.assertIsNotNone(repeats, "media_repeats() is not where this test looks for it")
        calls = "\n".join(f'    show("{name}", {name});' for name in EXPECTED)
        flags = subprocess.run([SDL2_CONFIG, "--cflags"], capture_output=True, text=True, check=True).stdout.split()
        with tempfile.TemporaryDirectory(prefix="being-media-keys-") as work:
            program, binary = Path(work) / "keys.c", Path(work) / "keys"
            program.write_text(HARNESS % (request, repeats, calls))
            built = subprocess.run([COMPILER, "-std=c11", "-Wall", "-Wextra", "-Werror", *flags, str(program),
                                    "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            output = subprocess.run([str(binary)], capture_output=True, text=True, check=True).stdout
        return {name: (request, repeat == "1") for name, request, repeat in
                (line.split() for line in output.splitlines())}

    def test_every_printed_key_leaves_the_right_request(self):
        got = self.run_harness()
        for name, want in EXPECTED.items():
            request, _ = got[name]
            self.assertEqual(request, {None: "none", "": "-"}.get(want, want), name)

    def test_level_keys_repeat_and_toggles_do_not(self):
        got = self.run_harness()
        for name, (request, repeats) in got.items():
            if request in ("none", "-"):
                continue
            self.assertEqual(repeats, request.endswith(("-up", "-down")), name)
        self.assertFalse(got["SDL_SCANCODE_MUTE"][1], "mute toggles once per press")
        self.assertFalse(got["SDL_SCANCODE_KBDILLUMTOGGLE"][1])
        self.assertTrue(got["SDL_SCANCODE_VOLUMEUP"][1])


@unittest.skipUnless(SDL2_CONFIG and COMPILER, "needs a C compiler and the SDL2 headers (sdl2-config)")
class ConsoleBuilds(unittest.TestCase):
    def test_the_whole_console_still_builds(self):
        from lab import console_preview as preview

        with tempfile.TemporaryDirectory(prefix="being-console-keys-") as work:
            try:
                preview.build(work)
            except Exception as error:  # the message says what the compiler said
                self.fail(f"being-console did not build: {error}")


if __name__ == "__main__":
    unittest.main()

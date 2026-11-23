"""being-console's key mapping, compiled on its own against the SDL2 headers.

Esc must reach the typing chat (mind/type_chat.py leaves on a lone Esc), and
on the framebuffer (KMS) every typed key comes through key_character().
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "os/package/being/being-console.c"
SDL2_CONFIG = shutil.which("sdl2-config") or next(
    (path for path in ("/opt/homebrew/bin/sdl2-config", "/usr/local/bin/sdl2-config") if Path(path).exists()), None)
COMPILER = shutil.which("cc")

HARNESS = r"""
#include <SDL.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

%s

static int key(SDL_Keycode code, Uint16 mod)
{
    SDL_Keysym sym = {0};
    sym.sym = code;
    sym.mod = mod;
    return (unsigned char)key_character(sym);
}

int main(void)
{
    printf("%%d %%d %%d %%d %%d %%d %%d\n", key(SDLK_ESCAPE, 0), key(SDLK_RETURN, 0), key(SDLK_BACKSPACE, 0),
           key(SDLK_a, 0), key(SDLK_a, KMOD_LSHIFT), key(SDLK_KP_5, 0), key(SDLK_F1, 0));
    return 0;
}
"""


def function(source, name):
    match = re.search(rf"^static char {name}\(SDL_Keysym key\)\n\{{\n.*?^\}}\n", source, re.M | re.S)
    return match[0] if match else None


class ConsoleSource(unittest.TestCase):
    def test_escape_is_passed_on_with_enter_and_backspace_outside_kms(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertRegex(function(source, "key_character"), r"SDLK_ESCAPE\)\s*return '\\033';")
        # SDL_TEXTINPUT carries no Esc, so handle_key must pass it on even off KMS.
        condition = re.search(r"if \(kms \|\|(.*?)\) \{", source, re.S)[1]
        self.assertIn("SDLK_ESCAPE", condition)


@unittest.skipUnless(SDL2_CONFIG and COMPILER, "needs a C compiler and the SDL2 headers (sdl2-config)")
class ConsoleKeys(unittest.TestCase):
    def test_key_character_maps_escape_enter_backspace_letters_and_the_keypad(self):
        source = SOURCE.read_text(encoding="utf-8")
        body = function(source, "key_character")
        self.assertIsNotNone(body, "key_character() is not where this test looks for it")
        flags = subprocess.run([SDL2_CONFIG, "--cflags"], capture_output=True, text=True, check=True).stdout.split()
        with tempfile.TemporaryDirectory(prefix="being-keys-") as work:
            program, binary = Path(work) / "keys.c", Path(work) / "keys"
            program.write_text(HARNESS % body)
            built = subprocess.run([COMPILER, "-std=c11", "-Wall", "-Wextra", "-Werror", *flags, str(program),
                                    "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            output = subprocess.run([str(binary)], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual([int(value) for value in output], [0o33, ord("\r"), 0o177, ord("a"), ord("A"), ord("5"), 0])


if __name__ == "__main__":
    unittest.main()

"""The hands organ's building blocks: what Pip types, the checks, grammars,
recipes, typist, OSC filter, cage line, forgetting and the panel frame spec."""

import ast
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mind import hands
from mind.hands import (
    NAME, RECIPES, Check, OscFilter, Ring, Typist, cage_command, check_python, check_shell_line,
    clear_workshop, end_word, forget_file, heredoc, name_stem, osc_flags, python_grammar,
    read_frame_header, remove_files, renderer_command, sanitise, shell_grammar, unique_name,
)

ROOT = Path(__file__).resolve().parent.parent


class SanitiseTest(unittest.TestCase):
    def test_drops_control_bytes_but_keeps_newlines(self):
        text = "a\x1b[31mb\x00c\x03d\x04e\x1af\x7fg\th\r\ni"
        self.assertEqual(sanitise(text), "a[31mbcdefg    h\ni")

    def test_drops_c1_and_bidi_overrides(self):
        self.assertEqual(sanitise("x\x9by‮z⁦w"), "xyzw")

    def test_limit(self):
        self.assertEqual(len(sanitise("x" * 10000)), hands.MAX_REQUEST)


class NamesTest(unittest.TestCase):
    def test_stem_from_request(self):
        self.assertEqual(name_stem("write a script that draws a spiral"), "draws_spiral")
        self.assertEqual(name_stem("counts down from 10"), "counts_down")
        self.assertEqual(name_stem("!!!"), "script")

    def test_never_overwrites(self):
        existing = {"spiral.py", "spiral-2.py"}
        self.assertEqual(unique_name("spiral", "py", existing.__contains__), "spiral-3.py")
        self.assertEqual(unique_name("Spiral!", "py", set().__contains__), "spiral.py")
        self.assertEqual(unique_name("9lives", "py", set().__contains__), "s9lives.py")

    def test_name_pattern(self):
        for good in ("a.py", "spiral-2.py", "notes.txt", "x_y.sh"):
            self.assertTrue(NAME.match(good), good)
        for bad in ("../a.py", "A.py", "a.exe", ".py", "a b.py", "a" * 30 + ".py"):
            self.assertFalse(NAME.match(bad), bad)


class HeredocTest(unittest.TestCase):
    def test_quoted_delimiter(self):
        word = end_word(random.Random(1))
        self.assertRegex(word, r"^PIP_END_[0-9a-f]{4}$")
        text = heredoc("spiral.py", "print('$HOME `x`')\n", word)
        self.assertEqual(text, f"cat > spiral.py <<'{word}'\nprint('$HOME `x`')\n{word}\n")

    def test_body_line_equal_to_end_word_is_refused(self):
        with self.assertRaises(ValueError):
            heredoc("a.py", "x = 1\nPIP_END_abcd\n", "PIP_END_abcd")

    def test_bad_name_refused(self):
        with self.assertRaises(ValueError):
            heredoc("../x.py", "x\n", "PIP_END_abcd")


class PythonCheckTest(unittest.TestCase):
    def test_clean_script(self):
        self.assertEqual(check_python("import time\nprint(time.time())\n"), Check(True))

    def test_free_import_fix(self):
        result = check_python("for i in range(3):\n    time.sleep(1)\n    print(math.pi)\n", "a.py")
        self.assertTrue(result.ok)
        self.assertEqual(result.missing, ("math", "time"))
        self.assertEqual(result.fixes, ("sed -i '1i import time' a.py", "sed -i '1i import math' a.py"))

    def test_assigned_names_are_not_missing(self):
        self.assertEqual(check_python("time = 3\nprint(time.real)\n").missing, ())

    def test_refusals(self):
        for source in ("import subprocess\n", "import socket\n", "eval('1')\n", "x = input()\n",
                       "import os\nos.system('ls')\n", "import shutil\nshutil.rmtree('x')\n",
                       "().__class__.__bases__\n", "from os import system\n", "from . import x\n"):
            result = check_python(source)
            self.assertFalse(result.ok, source)
            self.assertTrue(result.reason, source)

    def test_trailing_prose_is_trimmed_visibly(self):
        result = check_python("print(1)\nThis script prints one.\n", "a.py")
        self.assertTrue(result.ok)
        self.assertEqual(result.trimmed, 1)
        self.assertIn("sed -i '2,$d' a.py", result.fixes)

    def test_syntax_error(self):
        result = check_python("def (:\n")
        self.assertFalse(result.ok)
        self.assertIn("syntax", result.reason)


class ShellCheckTest(unittest.TestCase):
    def setUp(self):
        self.exists = {"notes.txt", "spiral.py"}.__contains__

    def test_allowed(self):
        for line in ("ls -l", "df -h .", "ls -l | head -5", "cat notes.txt | wc -l", "seq 1 5 > count.txt",
                     "echo 'hello there'", "python3 spiral.py", "cp -n notes.txt copy.txt", "date >> notes.txt",
                     "ls *.py"):
            self.assertEqual(check_shell_line(line, self.exists), "", line)

    def test_refused(self):
        for line in ("rm -rf /", "ls; rm x", "ls && ls", "echo $HOME", "echo `id`", "cat ../etc", "ls > notes.txt",
                     "find . -delete", "find . -exec cat {} +", "cp notes.txt b.txt", "python3 other.py",
                     "ls | head | tail | wc", "curl http://x", "cat < notes.txt", "mkdir /tmp/x", "ls || ls",
                     "echo 'open", "", "ls > /etc/passwd"):
            self.assertNotEqual(check_shell_line(line, self.exists), "", line)


class GrammarTest(unittest.TestCase):
    def test_python_grammar_shape(self):
        grammar = python_grammar(3)
        self.assertIn("root ::= r0", grammar)
        self.assertIn("r0 ::= line r1", grammar)  # at least one line
        self.assertIn("r1 ::= fence | line r2", grammar)
        self.assertIn("r3 ::= fence", grammar)
        self.assertIn('fence ::= "```"', grammar)
        self.assertNotIn("{", grammar)  # no repetition syntax: works on any GBNF version

    def test_resume_grammar(self):
        grammar = python_grammar(30, done=14, mid_line=True)
        self.assertTrue(grammar.startswith('root ::= [^`\\n]* "\\n" r15'))
        self.assertIn("r30 ::= fence", grammar)
        self.assertNotIn("r14 ::=", grammar)
        self.assertIn("root ::= r14", python_grammar(30, done=14))

    def test_every_rule_is_defined(self):
        for grammar in (python_grammar(30), python_grammar(30, done=5, mid_line=True), shell_grammar()):
            defined = set(re.findall(r"^([a-z0-9]+) ::=", grammar, re.M))
            used = set(re.findall(r"\b(r[0-9]+|line|fence|cmd|pipe|redir|applet|arg|word)\b", grammar))
            self.assertLessEqual(used, defined | {"root"}, grammar)

    def test_shell_grammar_lists_the_allowed_applets(self):
        grammar = shell_grammar()
        for applet in hands.APPLETS:
            self.assertIn(f'"{applet}"', grammar)
        self.assertNotIn('"rm"', grammar)


class RecipeTest(unittest.TestCase):
    """Every recipe is valid, passes Pip's own checks and prints what it says."""

    def run_script(self, source, cwd):
        # Sleeps are skipped so the tests stay fast; the code is otherwise as typed.
        wrapper = "import time\ntime.sleep = lambda s: None\nexec(compile(open('x.py').read(), 'x.py', 'exec'))\n"
        (Path(cwd) / "x.py").write_text(source)
        return subprocess.run([sys.executable, "-I", "-c", wrapper], cwd=cwd, capture_output=True, text=True,
                              timeout=20)

    def test_scripts_pass_checks_and_run(self):
        expected = {
            "countdown": "Lift off!", "countup": "Done counting.", "times_table": "12 x 7 =  84",
            "note": "Noted: buy milk", "list_notes": "notes in the workshop", "dice": "Total:", "coin": "!",
            "spiral": "#", "box": "+----------+", "triangle": "*****", "diamond": "*********", "tree": "|",
            "heart": "*", "clock": "", "warmth": "",
        }
        with tempfile.TemporaryDirectory() as cwd:
            for name, recipe in RECIPES.items():
                if recipe.kind != "script":
                    continue
                slots = {"text": "buy milk"} if name == "note" else {}
                source = recipe.build(slots)
                self.assertTrue(check_python(source).ok, name)
                self.assertEqual(check_python(source).fixes, (), name)
                self.assertLessEqual(source.count("\n"), 25, name)
                result = self.run_script(source, cwd)
                self.assertEqual(result.returncode, 0, (name, result.stderr))
                self.assertIn(expected[name], result.stdout, name)

    def test_note_text_cannot_escape(self):
        source = RECIPES["note"].build({"text": "x'); import os; os.system('id"})
        tree = ast.parse(source)
        self.assertEqual(check_python(source).ok, True)
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("system", calls)

    def test_slots_are_checked(self):
        with self.assertRaises(ValueError):
            RECIPES["countdown"].build({"n": 61})
        with self.assertRaises(ValueError):
            RECIPES["countdown"].build({"n": "10; rm"})
        self.assertIn("range(10, 0", RECIPES["countdown"].build({}))

    def test_command_recipes_pass_the_shell_check(self):
        for recipe in RECIPES.values():
            if recipe.kind == "command":
                self.assertEqual(check_shell_line(recipe.build({}), set().__contains__), "", recipe.name)

    def test_spiral_looks_like_a_spiral(self):
        with tempfile.TemporaryDirectory() as cwd:
            rows = self.run_script(RECIPES["spiral"].build({}), cwd).stdout.splitlines()
        self.assertEqual(len(rows), 21)
        middle = rows[10].replace(" ", "")
        self.assertGreater(sum(row.count("#") for row in rows), 150)
        self.assertTrue(middle)


class TypingBufferTest(unittest.TestCase):
    """The pty takes only part of a burst: the rest waits in order and a
    cancel's Ctrl-C drops the waiting text first."""

    class SlowPty:
        """Takes `room` bytes, then is full (returns 0) until room is given again."""

        def __init__(self, room):
            self.room = room
            self.got = b""

        def write(self, data):
            chunk = data[: self.room]
            self.room -= len(chunk)
            self.got += chunk
            return len(chunk)

    def make(self, room):
        organ = hands.Hands(bus=type("Bus", (), {})(), cage=None, cortex=None, disabled=None)
        organ.session = self.SlowPty(room)
        return organ

    def test_nothing_is_dropped_and_order_is_kept(self):
        organ = self.make(5)
        organ._type("print(1)\nprint(2)\n")
        self.assertEqual(organ.session.got, b"print")
        organ._type("x")
        for _ in range(10):
            organ.session.room = 3
            organ._flush_typing()
        self.assertEqual(organ.session.got, b"print(1)\rprint(2)\rx")
        self.assertEqual(organ._typing, b"")

    def test_an_urgent_key_drops_the_cancelled_text(self):
        organ = self.make(4)
        organ._type("cat > a.py <<'PIP_END_0000'\nbody\nPIP_END_0000\n")
        organ.session.room = 1
        organ._type("\x03", urgent=True)
        organ.session.room = 100
        organ._flush_typing()
        self.assertEqual(organ.session.got, b"cat \x03")


class TypistTest(unittest.TestCase):
    def drain(self, typist, start, end, step=0.04):
        out, now, times = "", start, []
        while now <= end:
            chunk = typist.take(now)
            if chunk:
                times.append((now, chunk))
            out += chunk
            now = round(now + step, 6)
        return out, times

    def test_recipe_pace_is_about_thirty_a_second(self):
        typist = Typist(fixed_rate=30.0, now=0.0)
        typist.feed("x" * 300, 0.0)
        typist.end()
        out, _ = self.drain(typist, 0.0, 5.0)
        self.assertAlmostEqual(len(out), 150, delta=10)

    def test_indentation_is_free_and_newline_costs_a_beat(self):
        typist = Typist(fixed_rate=10.0, now=0.0)
        typist.feed("a\n        b", 0.0)
        typist.end()
        first = typist.take(0.2)
        self.assertEqual(first, "a\n")
        self.assertEqual(typist.take(0.25), "")  # the beat after a line end
        rest = typist.take(0.5)
        self.assertTrue(rest.startswith("        "))

    def test_model_text_waits_for_the_jitter_buffer(self):
        typist = Typist(now=0.0)
        typist.feed("print(1)\n", 0.0, known=False)
        self.assertEqual(typist.take(0.5), "")
        out, _ = self.drain(typist, 0.84, 3.0)
        self.assertEqual(out, "print(1)\n")

    def test_known_text_is_not_delayed(self):
        typist = Typist(now=0.0)
        typist.feed("# you asked: x\n", 0.0, known=True)
        self.assertTrue(typist.take(0.1))

    def test_rate_tracks_arrival_within_bounds(self):
        typist = Typist(now=0.0)
        for step in range(50):  # 40 characters a second arriving
            typist.feed("abcd", step * 0.1, known=False)
        out, _ = self.drain(typist, 0.0, 5.0)
        self.assertGreater(len(out), 120)
        self.assertLessEqual(len(out), 200)

    def test_hold_pause_and_resume_keep_the_place(self):
        typist = Typist(fixed_rate=30.0, now=0.0)
        typist.feed("abcdefghij" * 10, 0.0)
        typist.end()
        before, _ = self.drain(typist, 0.0, 1.0)
        typist.hold()
        self.assertEqual(self.drain(typist, 1.04, 2.0)[0], "")
        typist.resume()
        after, _ = self.drain(typist, 2.04, 9.0)
        self.assertEqual(before + after, "abcdefghij" * 10)

    def test_human_typing_pauses_then_retypes_the_line(self):
        typist = Typist(fixed_rate=30.0, now=0.0)
        typist.feed("ls -l | head -5", 0.0)
        typist.end()
        typed, _ = self.drain(typist, 0.0, 0.3)
        typist.human(0.32)
        self.assertEqual(typist.take(0.5), "")
        self.assertEqual(typist.take(1.0), "")
        resumed = typist.take(0.32 + hands.HUMAN_QUIET + 0.01)
        self.assertTrue(resumed.startswith("\x15" + typed))

    def test_minimised_types_at_once(self):
        typist = Typist(now=0.0)
        typist.instant = True
        typist.feed("a" * 500, 0.0, known=False)
        self.assertEqual(len(typist.take(0.0)), 500)

    def test_queue_limit(self):
        typist = Typist(now=0.0)
        with self.assertRaises(OverflowError):
            typist.feed("x" * (hands.MAX_QUEUE + 1), 0.0)


class OscFilterTest(unittest.TestCase):
    def test_strips_flags_and_markers(self):
        clean, markers = OscFilter().feed(b"a\x1b]7770;pip=idle\x07b\x1b]7771;0\x07pip:~$ ")
        self.assertEqual(clean, b"abpip:~$ ")
        self.assertEqual(markers, ["0"])

    def test_split_across_reads(self):
        osc = OscFilter()
        parts = [b"x\x1b", b"]77", b"71;12", b"7\x07y"]
        out, markers = b"", []
        for part in parts:
            clean, found = osc.feed(part)
            out += clean
            markers += found
        self.assertEqual((out, markers), (b"xy", ["127"]))

    def test_st_terminator_and_other_oscs_pass(self):
        clean, markers = OscFilter().feed(b"\x1b]7771;1\x1b\\a\x1b]0;title\x07")
        self.assertEqual(markers, ["1"])
        self.assertEqual(clean, b"a\x1b]0;title\x07")

    def test_unterminated_flood_is_dropped(self):
        osc = OscFilter()
        clean, _ = osc.feed(b"ok\x1b]7770;" + b"x" * 600)
        self.assertEqual(clean, b"ok")
        self.assertEqual(osc.feed(b"next")[0], b"next")

    def test_plain_escape_at_end_is_held_only_if_it_could_start_a_marker(self):
        osc = OscFilter()
        self.assertEqual(osc.feed(b"a\x1b[")[0], b"a\x1b[")
        self.assertEqual(osc.feed(b"b\x1b")[0], b"b")
        self.assertEqual(osc.feed(b"[0m")[0], b"\x1b[0m")


class RingTest(unittest.TestCase):
    def test_bounded_and_wiped(self):
        ring = Ring(size=16)
        ring.add(b"0123456789")
        ring.add(b"abcdefghij")
        self.assertEqual(bytes(ring.data), b"456789abcdefghij")
        old = ring.data
        ring.wipe()
        self.assertEqual(bytes(old), b"\0" * 16)
        self.assertEqual(ring.lines(), [""])

    def test_lines_drop_escapes(self):
        ring = Ring()
        ring.add(b"\x1b[1;32mhello\x1b[0m\r\nworld\r\n")
        self.assertEqual(ring.lines(), ["hello", "world", ""])


class CageTest(unittest.TestCase):
    def test_fixed_command(self):
        argv = cage_command("being-shell-3", "/dev/pts/7")
        self.assertEqual(argv[0], "systemd-run")
        self.assertEqual(argv[-2:], ["/bin/sh", "-i"])
        joined = " ".join(argv)
        for needed in ("--uid=being-shell", "--slice=workshop.slice", "TTYPath=/dev/pts/7", "PrivateNetwork=yes",
                       "ProtectSystem=strict", "ReadWritePaths=/workshop", "TemporaryFileSystem=/run",
                       "InaccessiblePaths=-/mind", "CPUQuota=50%", "LimitNPROC=48", "NoNewPrivileges=yes",
                       "CPUSchedulingPolicy=idle", "ENV=/usr/lib/being/workshop/shellrc", "RuntimeMaxSec=4h"):
            self.assertIn(needed, joined)
        self.assertNotIn("ProcSubset=pid", joined)  # uptime and free read /proc
        # No history file: a forgotten script must not survive in ~/.ash_history.
        self.assertIn("--setenv=HISTFILE=/dev/null", argv)
        self.assertIn("HISTFILE=/dev/null", (ROOT / "os/package/being/being-hands-shellrc").read_text())

    def test_nothing_from_outside_reaches_the_command(self):
        for unit, tty in (("being-shell-1; rm", "/dev/pts/1"), ("being-shell-1", "/dev/pts/1 x"), ("x", "/dev/tty")):
            with self.assertRaises(ValueError):
                cage_command(unit, tty)

    def test_renderer_command(self):
        argv = renderer_command("terminal")
        self.assertIn("--uid=being-panel", argv)
        self.assertIn("--pipe", argv)
        self.assertEqual(argv[-7:], [hands.CONSOLE, "--panel", "terminal", "--size", "1280x640", "--io", "-"])
        with self.assertRaises(ValueError):
            renderer_command("bash")

    def test_systemd_cage_calls(self):
        calls = []

        def run(argv, timeout=10.0):
            calls.append(argv)
            if argv[:2] == ["systemctl", "show"]:
                return 0, "4242"
            return 0, ""
        cage = hands.SystemdCage(run=run)
        self.assertEqual(cage.start("being-shell-1", "/dev/pts/3", -1), 4242)
        cage.freeze("being-shell-1")
        cage.thaw("being-shell-1")
        cage.stop("being-shell-1")
        self.assertEqual(calls[0][0], "systemd-run")
        self.assertEqual(calls[2:], [["systemctl", "freeze", "being-shell-1"], ["systemctl", "thaw", "being-shell-1"],
                                     ["systemctl", "stop", "being-shell-1"]])

    def test_systemd_cage_kills_all_but_the_shell(self):
        with tempfile.TemporaryDirectory() as root:
            unit = Path(root) / "being-shell-1.service"
            unit.mkdir()
            child = subprocess.Popen(["sleep", "30"])
            try:
                (unit / "cgroup.procs").write_text(f"{os.getpid()}\n{child.pid}\n")
                cage = hands.SystemdCage(run=lambda argv, timeout=10: (0, ""), cgroup_root=root)
                self.assertEqual(cage.kill_all_but("being-shell-1", os.getpid()), 1)
                self.assertEqual(child.wait(timeout=5), -9)
            finally:
                child.kill()


class FlagsTest(unittest.TestCase):
    def test_osc_flags_are_clean(self):
        data = osc_flags(visible=True, pip="typing", status="Paused; \x1b[2J say carry on", title=None)
        self.assertEqual(data, b"\x1b]7770;visible=1;pip=typing;status=Paused, [2J say carry on\x07")
        self.assertEqual(osc_flags(), b"")


class ForgetTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_removes_file_copies_and_links_but_asks_about_changed_copies(self):
        (self.root / "spiral.py").write_text("print('spiral')\n")
        (self.root / "copy.py").write_text("print('spiral')\n")
        (self.root / "sub").mkdir()
        os.link(self.root / "spiral.py", self.root / "sub" / "linked.py")
        (self.root / "spiral-2.py").write_text("print('changed')\n")
        (self.root / "other.py").write_text("print('other')\n")
        outside = Path(self.dir.name + "-outside")
        outside.write_text("keep me")
        try:
            (self.root / "trap.py").symlink_to(outside)
            removed, changed = forget_file(self.root, "spiral.py")
            self.assertEqual(removed, ["copy.py", "spiral.py", "sub/linked.py"])
            self.assertEqual(changed, ["spiral-2.py"])
            self.assertEqual(outside.read_text(), "keep me")  # never followed the planted link
            self.assertTrue((self.root / "other.py").exists())
            removed, changed = forget_file(self.root, "spiral.py", also=("spiral-2.py",))
            self.assertEqual(removed, ["spiral-2.py"])
        finally:
            outside.unlink()

    def test_overwrites_before_unlinking(self):
        path = self.root / "a.py"
        path.write_text("secret\n")
        keep = os.open(path, os.O_RDONLY)  # a second handle sees the zeros
        try:
            forget_file(self.root, "a.py")
            self.assertFalse(path.exists())
            os.lseek(keep, 0, 0)
            self.assertEqual(os.read(keep, 100), b"\0" * 7)
        finally:
            os.close(keep)

    def test_a_planted_fifo_never_blocks_root(self):
        # A blocking open of a FIFO with no writer would hang hands for ever.
        os.mkfifo(self.root / "spiral.py")
        os.mkfifo(self.root / "copy.py")
        (self.root / "real.py").write_text("print(1)\n")
        done = threading.Event()
        result = []

        def forget():
            result.append(forget_file(self.root, "spiral.py"))
            done.set()
        threading.Thread(target=forget, daemon=True).start()
        self.assertTrue(done.wait(5), "forget_file blocked on a FIFO")
        self.assertEqual(result[0], ([], []))
        self.assertTrue((self.root / "real.py").exists())
        self.assertEqual(remove_files(self.root, ["copy.py", "../outside", "/etc/passwd"]), ["copy.py"])

    def test_remove_files_takes_only_workshop_names(self):
        (self.root / "sub").mkdir()
        (self.root / "sub" / "a.py").write_text("x")
        (self.root / "b.py").write_text("y")
        outside = Path(self.dir.name + "-outside")
        outside.write_text("keep me")
        try:
            (self.root / "trap.py").symlink_to(outside)
            self.assertEqual(remove_files(self.root, ["sub/a.py", "trap.py", "missing.py"]), ["sub/a.py", "trap.py"])
            self.assertEqual(outside.read_text(), "keep me")
            self.assertTrue((self.root / "b.py").exists())
        finally:
            outside.unlink()

    def test_clear_workshop(self):
        (self.root / "apps" / "snake").mkdir(parents=True)
        (self.root / "apps" / "snake" / "main.py").write_text("x")
        (self.root / "a.txt").write_text("y")
        (self.root / "link").symlink_to("/etc")
        self.assertEqual(clear_workshop(self.root), 3)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertTrue(Path("/etc").exists())


class PanelFrameTest(unittest.TestCase):
    """The C header (os/package/being/being-panel-frame.h) and the Python
    reference reader agree on the frame layout and the OSC keys."""

    HEADER = ROOT / "os/package/being/being-panel-frame.h"

    def test_python_layout(self):
        self.assertEqual(hands.FRAME_HEADER.size, 128)

    def test_header_constants_match(self):
        text = self.HEADER.read_text()
        for name, bit in hands.FRAME_FLAGS.items():
            c_name = {"visible": "BP_VISIBLE", "focus": "BP_FOCUS", "pip_typing": "BP_PIP_TYPING",
                      "pip_paused": "BP_PIP_PAUSED", "job_busy": "BP_JOB_BUSY", "human_typing": "BP_HUMAN_TYPING",
                      "scrolled": "BP_SCROLLED"}[name]
            shift = bit.bit_length() - 1
            self.assertIn(f"{c_name} = 1u << {shift}", text)
        for key in ("visible", "focus", "pip", "job", "human", "title", "status", "scroll", "wipe"):
            self.assertIn(f'"{key}"', text)

    @unittest.skipUnless(shutil.which("cc"), "no C compiler")
    def test_compiled_writer_is_read_by_python(self):
        program = r'''
#include <stdio.h>
#include <stdlib.h>
#include "being-panel-frame.h"
int main(int argc, char **argv) {
    size_t size = being_panel_file_size(4, 3);
    struct being_panel_header *frame = calloc(1, size);
    uint8_t rows[2 * 16];
    being_panel_init(frame, 4, 3);
    for (int i = 0; i < 32; ++i) rows[i] = (uint8_t)i;
    being_panel_begin(frame);
    being_panel_rows(frame, 1, 2, rows, 16);
    being_panel_osc(frame, "visible", "1");
    being_panel_osc(frame, "pip", "typing");
    being_panel_osc(frame, "job", "busy");
    being_panel_osc(frame, "title", "Pip's terminal");
    being_panel_osc(frame, "status", "Running in 4 \xc2\xb7 Esc or say stop and more text here");
    int act = being_panel_osc(frame, "wipe", "1");
    frame->beat = 9; frame->cursor_x = 2; frame->cursor_y = 1;
    being_panel_end(frame);
    FILE *out = fopen(argv[1], "wb");
    fwrite(frame, 1, size, out);
    fclose(out);
    return act == BP_ACT_WIPE ? 0 : 3;
}
'''
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "writer.c"
            source.write_text(program)
            binary = Path(work) / "writer"
            build = subprocess.run(["cc", "-std=c11", "-Wall", "-Werror", "-I", str(self.HEADER.parent),
                                    "-o", str(binary), str(source)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            frame = Path(work) / "terminal.frame"
            self.assertEqual(subprocess.run([str(binary), str(frame)]).returncode, 0)
            raw = frame.read_bytes()
        header = read_frame_header(raw)
        self.assertEqual(len(raw), 128 + 4 * 4 * 3)
        self.assertEqual((header["width"], header["height"], header["stride"]), (4, 3, 16))
        self.assertEqual(header["sequence"], 2)
        self.assertEqual(header["dirty"], (1, 2))
        self.assertEqual(header["flags"], {"visible", "pip_typing", "job_busy"})
        self.assertEqual(header["title"], "Pip's terminal")
        self.assertTrue(header["status"].startswith("Running in 4 ·"))
        self.assertLessEqual(len(header["status"].encode()), 39)
        self.assertEqual(raw[128 + 16:128 + 48], bytes(range(32)))
        self.assertEqual((header["beat"], header["cursor"]), (9, (2, 1)))

    def test_reader_refuses_a_frame_being_written(self):
        raw = bytearray(hands.FRAME_HEADER.pack(b"BPN1", 1, 4, 3, 16, 0, 3, 0, 0, 0, -1, -1, 0, b"", b""))
        self.assertIsNone(read_frame_header(bytes(raw)))
        raw = hands.FRAME_HEADER.pack(b"BPN1", 1, 4, 3, 16, 0, 4, 2, 5, 0, -1, -1, 0, b"", b"")
        self.assertIsNone(read_frame_header(raw))  # dirty rows past the end


class ShelfTest(unittest.TestCase):
    SHELF = ROOT / "mind/workshop_shelf"

    def test_five_to_eight_programs_with_true_manifests(self):
        manifests = sorted(self.SHELF.glob("*/app.json"))
        self.assertTrue(5 <= len(manifests) <= 8)
        total = 0
        for manifest in manifests:
            info = json.loads(manifest.read_text())
            source = manifest.parent / info["entry"]
            self.assertEqual(info["name"], manifest.parent.name)
            self.assertEqual(info["size"], source.stat().st_size, info["name"])
            self.assertEqual(info["sha256"], hashlib.sha256(source.read_bytes()).hexdigest(), info["name"])
            self.assertTrue(info["aliases"] and info["summary"] and info["keys"])
            total += info["size"]
            tree = ast.parse(source.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name, ("os", "random", "select", "string", "sys", "time", "termios", "tty"))
        self.assertLess(total, 200 * 1024)

    def run_program(self, name, *args, stdin=""):
        return subprocess.run([sys.executable, "-I", str(self.SHELF / name / "main.py"), *args], input=stdin,
                              capture_output=True, text=True, timeout=30)

    def test_programs_run_without_a_keyboard(self):
        self.assertIn("Game over", self.run_program("snake", "--frames", "30").stdout)
        self.assertIn("Life stopped", self.run_program("life", "--frames", "5").stdout)
        self.assertIn("rain stopped", self.run_program("matrix", "--frames", "5").stdout)
        self.assertIn("Stopped at", self.run_program("stopwatch", "--for", "0.3").stdout)
        dice = self.run_program("dice", "3", "--seed", "1", stdin="\nq\n").stdout
        self.assertEqual(dice.count("total"), 2)

    def test_guess_and_hangman_play_to_the_end(self):
        sys.path.insert(0, str(self.SHELF / "guess"))
        try:
            import importlib
            guess = importlib.import_module("main")
            said = []
            self.assertEqual(guess.play(42, iter(["50", "x", "25", "42"]), said.append), 3)
            self.assertEqual(said[1:4], ["Lower.", "Type a whole number, then Enter.", "Higher."])
        finally:
            sys.path.pop(0)
            sys.modules.pop("main", None)
        out = self.run_program("hangman", "--seed", "2", stdin="\n".join("abcdefghijklmnopqrstuvwxyz") + "\n").stdout
        self.assertTrue("You got it" in out or "Out of lives" in out)

    def test_install_run_uninstall(self):
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "BEING_SHELF": str(self.SHELF), "PATH": os.environ["PATH"]}
            shelf = [sys.executable, "-I", str(self.SHELF / "shelf.py")]

            def call(*args):
                return subprocess.run(shelf + list(args), env=env, capture_output=True, text=True, timeout=30)
            first = call("install", "snake")
            self.assertEqual(first.returncode, 0)
            self.assertIn("No internet was used", first.stdout)
            self.assertTrue((Path(home) / "apps/snake/main.py").is_file())
            self.assertIn("already installed", call("install", "the snake game").stdout)
            self.assertIn("Game over", call("run", "snake", "--frames", "3").stdout)
            refused = call("install", "firefox")
            self.assertEqual(refused.returncode, 1)
            self.assertIn("can't download", refused.stdout)
            (Path(home) / "apps/snake/main.py").write_text("print('faster')\n")
            self.assertIn("has been changed", call("install", "snake").stdout)
            self.assertIn("Uninstalled snake", call("uninstall", "snake").stdout)
            self.assertIn("isn't installed", call("run", "snake").stdout)
            self.assertIn("snake", call("list").stdout)

    def test_install_refuses_a_planted_link(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as elsewhere:
            (Path(home) / "apps").mkdir()
            (Path(home) / "apps" / "snake").symlink_to(elsewhere)
            env = {"HOME": home, "BEING_SHELF": str(self.SHELF), "PATH": os.environ["PATH"]}
            out = subprocess.run([sys.executable, "-I", str(self.SHELF / "shelf.py"), "install", "snake"], env=env,
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1)
            self.assertEqual(list(Path(elsewhere).iterdir()), [])

    def test_wrappers(self):
        for helper in ("install", "uninstall", "shelf", "run"):
            path = self.SHELF / "bin" / helper
            self.assertTrue(os.access(path, os.X_OK), helper)
            self.assertIn("/usr/share/being/workshop/shelf", path.read_text())


class UnitTest(unittest.TestCase):
    """os/package/being/being-hands.service (PIP-FUNCTIONS L9.4)."""

    def values(self, key):
        text = (ROOT / "os/package/being/being-hands.service").read_text()
        lines = [line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")]
        return [line.split("=", 1)[1] for line in lines if line.startswith(key + "=")]

    def test_a_light_weight_and_precompiled_bytecode(self):
        self.assertEqual(self.values("CPUWeight"), ["20"])
        self.assertEqual(self.values("Nice"), ["10"])
        environment = " ".join(self.values("Environment"))
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", environment)
        self.assertIn("PYTHONPATH=/usr/lib/being", environment)
        self.assertEqual(self.values("ProtectSystem"), ["strict"], "the shipped .pyc can never be rewritten")

    def test_the_monitor_and_clock_read_only_what_hands_may(self):
        # The clock reads /mind/reminders.db and the monitor statvfs(/mind):
        # read-only, which the unit already guarantees.
        self.assertEqual(self.values("ReadOnlyPaths"), ["/mind"])
        self.assertEqual(self.values("ReadWritePaths"), ["/workshop"])


class RendererSignalTest(unittest.IsolatedAsyncioTestCase):
    async def test_show_and_hide_go_through_systemd(self):
        calls = []

        def run(argv, timeout=10.0):
            calls.append(argv)
            return 0, ""
        renderer = hands.PanelRenderer("monitor", run=run)
        self.assertFalse(await renderer.hide(), "no renderer running: nothing is signalled")
        self.assertEqual(calls, [])

        class Running:
            returncode = None
        renderer.proc = Running()
        self.assertTrue(await renderer.hide())
        self.assertFalse(renderer.visible)
        self.assertTrue(await renderer.show())
        self.assertTrue(renderer.visible)
        self.assertEqual(calls, [
            ["systemctl", "kill", "--kill-whom=main", "--signal=SIGUSR2", "being-panel-monitor.service"],
            ["systemctl", "kill", "--kill-whom=main", "--signal=SIGUSR1", "being-panel-monitor.service"],
        ])
        failing = hands.PanelRenderer("clock", run=lambda argv, timeout=10.0: (1, ""))
        failing.proc = Running()
        self.assertFalse(await failing.hide())
        self.assertTrue(failing.visible, "a refused signal changes nothing")

    async def test_ready_waits_for_the_first_frame_file(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(hands, "PANEL_DIR", Path(folder)):
            renderer = hands.PanelRenderer("clock", run=lambda argv, timeout=10.0: (0, ""))

            class Running:
                returncode = None
            self.assertFalse(await renderer.ready(0.05), "no renderer: never ready")
            renderer.proc = Running()
            self.assertFalse(await renderer.ready(0.05, step=0.01), "no frame yet: gives up at the timeout")

            async def appear():
                await asyncio.sleep(0.03)
                (Path(folder) / "clock.frame").write_bytes(b"BPF1")
            task = asyncio.get_running_loop().create_task(appear())
            self.assertTrue(await renderer.ready(1.0, step=0.01))
            await task

    def test_every_app_has_a_renderer_line(self):
        for app in hands.APPS:
            self.assertEqual(renderer_command(app)[-5], app)
        self.assertEqual(set(hands.SMALL_APPS), {"monitor", "clock"})
        from mind import app_clock, app_monitor
        self.assertEqual(hands.SMALL_TITLES, {"monitor": app_monitor.TITLE, "clock": app_clock.TITLE})
        self.assertEqual((app_monitor.COLUMNS, app_monitor.ROWS), (hands.GRID_COLUMNS, hands.GRID_ROWS))
        self.assertEqual((app_clock.COLUMNS, app_clock.ROWS), (hands.GRID_COLUMNS, hands.GRID_ROWS))


if __name__ == "__main__":
    unittest.main()

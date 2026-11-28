"""being-console --panel: the workshop renderer's frame file (voice-apps.md 3.2).

Builds being-console against the host SDL2 (as lab/console_preview.py does) and
drives panel mode the way hands will: bytes and OSC 7770 control strings on its
input, SIGUSR1/SIGUSR2 to show and hide, end of input to close. Skipped where
there is no C compiler or sdl2-config.
"""

import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lab import console_preview as preview
from mind import app_calc, app_organizer, hands
from mind.lists import Lists

BUILD = None
SKIP = None


def setUpModule():
    global BUILD, SKIP
    if not shutil.which("cc") or not shutil.which("sdl2-config"):
        SKIP = "needs cc and sdl2-config"
        return
    BUILD = tempfile.TemporaryDirectory(prefix="being-console-test-")
    try:
        preview.build(BUILD.name)
    except (subprocess.CalledProcessError, OSError) as error:
        SKIP = f"being-console did not build: {error}"


def tearDownModule():
    if BUILD:
        BUILD.cleanup()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out")


class ConsoleInstallEyes(unittest.TestCase):
    def test_eyes_open_with_bounded_install_progress_and_reset_afterward(self):
        if SKIP:
            self.skipTest(SKIP)

        def header(control):
            command = [sys.executable, "-c", f"import os, time; os.write(1, {control!r}); time.sleep(1)"]
            width, _, pixels = preview.capture(command, size=(640, 400), wait=0.25, build_dir=BUILD.name, panel=False)
            return pixels[:width * 48 * 3]

        def cyan(pixels):
            return sum(green >= 140 and blue >= 170 for green, blue in zip(pixels[1::3], pixels[2::3]))

        initial = header(b"")
        early = header(b"\x1b]7772;install=20\x07")
        late = header(b"\x1b]7772;install=80\x07")
        self.assertLess(cyan(early), cyan(late))
        self.assertLess(cyan(late), cyan(initial))
        self.assertEqual(header(b"\x1b]7772;install=20\x07\x1b]7772;install=101\x07"), early)
        self.assertEqual(header(b"\x1b]7772;install=20\x07\x1b]7772;install=off\x07"), initial)


class PanelTest(unittest.TestCase):
    def setUp(self):
        if SKIP:
            self.skipTest(SKIP)

    def session(self, *extra, size=(1280, 640), name="terminal"):
        session = preview.PanelSession(None, size, name=name, build_dir=BUILD.name, extra=extra)
        self.addCleanup(session.__exit__)
        return session

    def settle(self, session, seconds=0.15):
        time.sleep(seconds)
        return session.read()

    def test_header_describes_the_frame(self):
        session = self.session()
        header, pixels = session.read()
        self.assertEqual((header["width"], header["height"], header["stride"]), (1280, 640, 1280 * 4))
        self.assertEqual((header["columns"], header["rows"]), (107, 23))
        self.assertEqual(header["cell"], (11, 24))
        self.assertEqual(header["title"], "Terminal")
        self.assertEqual(header["pid"], session.renderer.pid)
        self.assertEqual(header["flags"], {"visible", "focus"})
        self.assertEqual(header["sequence"] % 2, 0)
        self.assertEqual(len(pixels), 1280 * 640 * 4)
        self.assertTrue(all(alpha == 255 for alpha in pixels[3::4 * 97]))
        mode = stat.S_IMODE(session.frame.stat().st_mode)
        self.assertEqual(mode & 0o027, 0, oct(mode))
        self.assertEqual(sorted(path.name for path in session.directory.iterdir()), ["terminal.frame"])

    def test_control_strings_set_flags_title_and_status(self):
        session = self.session()
        _, before = session.read()
        session.control("pip=typing", "job=busy", "human=1", "title=My shell ✓")
        header, after = wait_for(lambda: (lambda h: h if "pip_typing" in h[0]["flags"] else None)(session.read()))
        self.assertEqual(header["flags"], {"visible", "focus", "pip_typing", "job_busy", "human_typing"})
        self.assertEqual(header["title"], "My shell ✓")
        bar = 1280 * 4 * 28
        self.assertNotEqual(before[:bar], after[:bar], "the status bar shows the change")
        session.control("pip=paused", "job=idle", "human=0", "focus=0")
        header, _ = wait_for(lambda: (lambda h: h if "pip_paused" in h[0]["flags"] else None)(session.read()))
        self.assertEqual(header["flags"], {"visible", "pip_paused"})
        session.control("title=" + "x" * 40, "status=Running 0:12 · Esc stops")
        header, _ = wait_for(lambda: (lambda h: h if h[0]["title"].startswith("xx") else None)(session.read()))
        self.assertEqual(header["title"], "x" * 31)

    def test_calculator_changes_native_bpn_pixels_and_accepts_focus(self):
        session = self.session("--title", "Calculator", name="calc")
        calc = app_calc.Calculator()
        session.send(calc.render())
        before, pixels = wait_for(lambda: (lambda frame: frame if frame[0]["sequence"] > 2 else None)(session.read()))
        self.assertEqual(before["flags"], {"visible", "focus"})
        self.assertEqual(before["title"], "Calculator")
        for key in b"17*23\r":
            calc.key(bytes((key,)))
        session.send(calc.render())
        after, pixels_after = wait_for(
            lambda: (lambda frame: frame if frame[0]["sequence"] > before["sequence"] and frame[1] != pixels else None)(
                session.read()))
        self.assertEqual(calc.last, ("17*23", "391", False))
        self.assertIn("focus", after["flags"])
        self.assertNotEqual(pixels, pixels_after)

    def test_lists_wipes_the_native_frame_and_has_focus(self):
        with tempfile.TemporaryDirectory() as state:
            Lists(state).add("shopping", ["Synthetic owner item"])
            session = self.session("--title", "Lists", name="lists")
            initial, blank = session.read()
            session.send(app_organizer.render("lists", app_organizer.read_page(state, "lists")))
            shown, pixels = wait_for(lambda: (lambda frame: frame if frame[1] != blank else None)(session.read()))
        self.assertEqual(shown["title"], "Lists")
        self.assertIn("focus", shown["flags"])
        self.assertGreater(shown["sequence"], initial["sequence"])
        session.control("wipe=1")
        _, cleared = wait_for(lambda: (lambda frame: frame if frame[1] != pixels else None)(session.read()))
        self.assertEqual(cleared, blank)

    def test_a_program_cannot_reach_the_control_channel(self):
        state = {"osc": False, "escape": False}
        out = preview.strip_osc(b"a\x1b]7770;pip=typ", state) + preview.strip_osc(b"ing\x07b\x1b[1mc", state)
        self.assertEqual(out, b"ab\x1b[1mc")

    def test_cursor_and_newest_line(self):
        session = self.session()
        session.send(b"one\r\ntwo\r\nthree")
        header, _ = wait_for(lambda: (lambda h: h if h[0]["cursor"][0] > 100 else None)(session.read()))
        self.assertEqual(header["cursor"], (50 + 5 * 11, 34 + 2 * 24))
        self.assertEqual(header["newest_y"], 34 + 2 * 24)
        session.send(b"\x1b[?25l")
        header, _ = wait_for(lambda: (lambda h: h if h[0]["cursor"] == (-1, -1) else None)(session.read()))

    def test_only_changed_rows_are_written(self):
        session = self.session()
        self.settle(session, 0.6)
        start = session.read()[0]["sequence"]
        session.send(b"hello")
        header, _ = wait_for(lambda: (lambda h: h if h[0]["sequence"] > start and h[0]["dirty_count"] else None)(
            session.read()))
        self.assertEqual((header["dirty_first"], header["dirty_count"]), (34, 24))

    def test_incremental_frames_match_a_full_redraw(self):
        steps = [b"\x1b[44m  box one  \x1b[0m\r\n", b"\x1b[44m  box two  \x1b[0m plain\r\n",
                 b"\x1b[44m    \x1b[0m\r\n", b"\x1b[1;32mok\x1b[0m \xe2\x9c\x93\r\n", b"\x1b[2A\x1b[K", b"tail"]
        stepped = self.session()
        for step in steps:
            stepped.send(step)
            time.sleep(0.06)
        whole = self.session()
        whole.send(b"".join(steps))
        _, incremental = self.settle(stepped, 0.3)
        _, full = self.settle(whole, 0.3)
        self.assertEqual(incremental, full)

    def test_frames_are_capped_at_30_a_second(self):
        session = self.session()
        start = session.read()[0]["sequence"]
        began = time.monotonic()
        for index in range(400):
            session.send(f"line {index}\r\n".encode())
            time.sleep(0.0025)
        elapsed = time.monotonic() - began
        end = self.settle(session, 0.1)[0]["sequence"]
        frames = (end - start) // 2
        self.assertGreater(frames, 5)
        self.assertLessEqual(frames, 30 * (elapsed + 0.1) + 2 * elapsed + 4, f"{frames} frames in {elapsed:.2f} s")

    def test_beat_while_visible_and_quiet_while_hidden(self):
        session = self.session()
        first = session.read()[0]["beat"]
        time.sleep(1.15)
        self.assertGreaterEqual(session.read()[0]["beat"], first + 2)

        session.renderer.send_signal(signal.SIGUSR2)
        header, _ = wait_for(lambda: (lambda h: h if "visible" not in h[0]["flags"] else None)(session.read()))
        self.assertNotIn("focus", header["flags"])
        session.send(b"output while minimised\r\n")
        time.sleep(1.2)
        hidden = session.read()[0]
        self.assertEqual((hidden["beat"], hidden["sequence"]), (header["beat"], header["sequence"]),
                         "hidden: no beat and no frames")
        session.control("pip=typing")
        typing = wait_for(lambda: (lambda h: h if "pip_typing" in h[0]["flags"] else None)(session.read()))[0]
        self.assertEqual(typing["dirty_count"], 0, "flags still arrive while minimised")
        self.assertNotIn("visible", typing["flags"])

        session.renderer.send_signal(signal.SIGUSR1)
        shown, _ = wait_for(lambda: (lambda h: h if "visible" in h[0]["flags"] else None)(session.read()))
        self.assertEqual((shown["dirty_first"], shown["dirty_count"]), (0, 640))
        self.assertGreater(shown["beat"], hidden["beat"])

    def test_wipe_forgets_screen_scrollback_and_pixels(self):
        blank = self.session()
        _, empty = blank.read()
        session = self.session()
        session.send(b"".join(b"secret %d\r\n" % index for index in range(60)))
        _, dirty = self.settle(session)
        self.assertNotEqual(dirty, empty)
        session.control("wipe=1")
        session.control("scroll=up")
        header, wiped = self.settle(session, 0.3)
        self.assertNotIn("scrolled", header["flags"], "no scrollback is left to scroll into")
        self.assertEqual(wiped, empty, "not a pixel of the old text is left in the frame file")

    def test_wipe_while_minimised_clears_the_frame_at_once(self):
        blank = self.session()
        _, empty = blank.read()
        session = self.session()
        session.send(b"".join(b"secret %d\r\n" % index for index in range(5)))
        _, dirty = self.settle(session, 0.3)
        self.assertNotEqual(dirty, empty)
        session.renderer.send_signal(signal.SIGUSR2)
        wait_for(lambda: "visible" not in session.read()[0]["flags"])
        session.control("wipe=1")
        header, wiped = wait_for(lambda: (lambda h: h if h[1] == empty else None)(session.read()), timeout=2)
        self.assertNotIn("visible", header["flags"], "forgetting does not show the panel")
        session.control("title=Renamed")
        header = wait_for(lambda: (lambda h: h if h["title"] == "Renamed" else None)(session.read()[0]))
        self.assertNotIn("visible", header["flags"])

    def test_a_half_sequence_cannot_swallow_a_control_string(self):
        blank = self.session()
        _, empty = blank.read()
        session = self.session()
        session.send(b"secret\r\n\x1b(")  # a program stopped after ESC (
        self.settle(session, 0.2)
        session.control("wipe=1")
        _, wiped = wait_for(lambda: (lambda h: h if h[1] == empty else None)(session.read()), timeout=2)
        self.assertEqual(wiped, empty)

    def test_a_trailing_escape_escape_cannot_swallow_a_wipe(self):
        # hands' OscFilter holds back only a chunk's last ESC, so when a
        # program ends in ESC ESC the first reaches the renderer just before
        # hands' own control string does.
        filtered = hands.OscFilter().feed(b"secret\r\n\x1b\x1b")[0]
        self.assertEqual(filtered, b"secret\r\n\x1b")
        blank = self.session()
        _, empty = blank.read()
        for output in (filtered, b"secret\r\n\x1b\x1b"):
            with self.subTest(output=output):
                session = self.session()
                session.send(output)
                self.settle(session, 0.2)
                session.control("wipe=1")
                _, wiped = wait_for(lambda: (lambda h: h if h[1] == empty else None)(session.read()), timeout=2)
                self.assertEqual(wiped, empty, "']7770;wipe=1' was drawn as text and the old output stayed")

    def test_cancel_ends_any_half_sequence(self):
        plain = self.session()
        plain.send(b"Z")
        _, expected = self.settle(plain, 0.3)
        for prefix in (b"\x1b", b"\x1b[12", b"\x1b(", b"\x1b]0;half a title"):
            for cancel in (b"\x18", b"\x1a"):
                with self.subTest(prefix=prefix, cancel=cancel):
                    session = self.session()
                    session.send(prefix + cancel + b"Z")
                    _, pixels = self.settle(session, 0.3)
                    self.assertEqual(pixels, expected, "the Z after CAN/SUB is drawn as text")

    def test_erase_scrollback_is_real_in_panel_mode(self):
        session = self.session()
        session.send(b"".join(b"line %d\r\n" % index for index in range(60)))
        session.control("scroll=up")
        header, _ = wait_for(lambda: (lambda h: h if "scrolled" in h[0]["flags"] else None)(session.read()))
        self.assertEqual(header["cursor"], (-1, -1))
        session.control("scroll=end")
        wait_for(lambda: "scrolled" not in session.read()[0]["flags"])
        session.send(b"\x1b[3J")
        session.control("scroll=up")
        header = self.settle(session, 0.3)[0]
        self.assertNotIn("scrolled", header["flags"])

    def test_end_of_input_removes_the_frame(self):
        session = self.session()
        self.assertTrue(session.frame.exists())
        directory = session.directory
        status = session.renderer.poll()
        self.assertIsNone(status)
        os.close(session._write)
        self.assertEqual(session.renderer.wait(timeout=5), 0)
        self.assertFalse((directory / "terminal.frame").exists())

    def test_terminate_removes_the_frame(self):
        session = self.session()
        session.renderer.send_signal(signal.SIGTERM)
        self.assertEqual(session.renderer.wait(timeout=5), 0)
        self.assertFalse(session.frame.exists())

    def test_pty_session_and_small_size(self):
        with preview.PanelSession(["/bin/sh", "-c", "printf 'hi \\033[32mgreen\\033[0m\\n'; sleep 3"], (640, 300),
                                  build_dir=BUILD.name) as session:
            session.pump(0.6)
            header, pixels = session.read()
            self.assertEqual((header["width"], header["height"]), (640, 300))
            self.assertEqual(header["cell"], (8, 16))
            self.assertEqual(len(pixels), 640 * 300 * 4)
            self.assertGreaterEqual(header["newest_y"], 0)

    def test_bad_arguments_are_refused(self):
        binary = preview.build(BUILD.name)
        with tempfile.TemporaryDirectory() as directory:
            for arguments in (["--panel", "../x"], ["--panel", "Term"], ["--panel", "terminal", "--size", "9x9"],
                              ["--panel", "terminal", "--bogus", "1"]):
                result = subprocess.run([str(binary), *arguments, "--dir", directory], stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2, arguments)
            self.assertEqual(os.listdir(directory), [])
            result = subprocess.run([str(binary), "--panel", "terminal", "--dir", directory + "/missing"],
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1)

    def test_the_monitor_and_the_clock_draw_in_place_without_focus(self):
        # As hands draws them: a title, focus off (keys stay with the face),
        # then whole frames of positioned rows that never scroll.
        from mind import app_clock, app_monitor, hands
        for app, frame in (("monitor", preview.sample_monitor_frame()), ("clock", preview.sample_clock_frame())):
            with self.subTest(app=app):
                session = self.session(name=app)
                session.send(hands.osc_flags(title=hands.SMALL_TITLES[app], status="Live · once a second", focus=0,
                                             pip="idle", job="idle"))
                for _ in range(3):  # a frame a second: the same rows again
                    session.send(frame.encode())
                header, pixels = wait_for(lambda: (lambda h: h if h[0]["cursor"] == (-1, -1) else None)(session.read()))
                self.assertEqual(header["title"], hands.SMALL_TITLES[app])
                self.assertEqual(header["flags"], {"visible"}, "no focus, never scrolled")
                self.assertEqual((header["columns"], header["rows"]), (app_monitor.COLUMNS, app_monitor.ROWS))
                self.assertEqual((header["columns"], header["rows"]), (app_clock.COLUMNS, app_clock.ROWS))
                lit = sum(1 for value in pixels[0::4 * 61] if value > 128)
                self.assertGreater(lit, 50, "something is drawn")
                session.close()
                self.assertFalse(session.frame.exists(), "closing removes the picture")


if __name__ == "__main__":
    unittest.main()

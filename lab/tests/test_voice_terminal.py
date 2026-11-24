"""'Hey Pip, open a terminal.' said in one breath opens the workshop terminal.

The lab image, its ALSA loopback body and the L4 audio harness (test_l4.py):
generated speech goes in at the capture side of snd-aloop exactly as a
microphone's would, through the voice's gain, endpointing, wake spotting,
PocketSphinx dictation and the command grammar's second pass. For each
fixture this checks the wake, a final that names the terminal, the mind's
code route (`route {"route": "workshop.open"}`, no model turn), app.state
shown, the renderer unit, the frame's visible bit and the spoken "Here's my
terminal." (captured at the playback side and, when the image has the
pocketsphinx program, transcribed back).

The fixtures cover the laptop's problem: the same sentence at full level, 30 dB
and 45 dB down (with a quiet analog-like noise floor, as the MacBook's
microphone has), a wake and then the command, and a second voice. The report
prints the exact transcript and which recognition pass gave it.

    BEING_GATE=1 python3 -m unittest -v lab.tests.test_voice_terminal
"""

import json
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from lab.tests.test_apps import fresh_image
from lab.tests.test_l4 import EVENTS, AudioHarness

ROUTE = re.compile(r"\broute (\{.*\})\s*$")  # as lab/try_timings.py reads them

PHRASE = "Hey Pip, open a terminal."
FRAME_DIR = "/run/being-panel"
VISIBLE = 1
REPLY = "Here's my terminal."
# (fixture, voice, text, gain in dB, noise floor in dBFS or None)
ONE_BREATH = (
    ("terminal-full", "Samantha", PHRASE, 0.0, None),
    ("terminal-30db", "Samantha", PHRASE, -30.0, -66.0),
    ("terminal-45db", "Samantha", PHRASE, -45.0, -66.0),
    ("terminal-karen", "Karen", PHRASE, -12.0, -66.0),
)
COMMAND_ONLY = ("terminal-command", "Samantha", "Open a terminal.", -12.0, -66.0)

APP_WATCH = r"""
import json, socket, time
bus = socket.socket(socket.AF_UNIX)
bus.connect('/run/being/bus.sock')
bus.sendall(b'{"t":"hello","name":"voice-terminal-watch","sub":["app.state","app.refused"]}\n')
for line in bus.makefile():
    event = json.loads(line)
    event['monotonic'] = time.monotonic()
    print(json.dumps(event), flush=True)
"""

BUS_SEND = r"""
import json, socket, sys
with socket.socket(socket.AF_UNIX) as bus:
    bus.connect('/run/being/bus.sock')
    bus.sendall(b'{"t":"hello","name":"voice-terminal-send","sub":[]}\n')
    bus.sendall((sys.argv[1] + '\n').encode())
"""

# The frame header as the face reads it (crates/face/src/panel.rs): flags at 32.
FRAME_PROBE = r"""
import struct, sys
with open(sys.argv[1], 'rb') as frame:
    head = frame.read(128)
print(head[:4].decode(), struct.unpack_from('<I', head, 32)[0])
"""


def make_fixture(path, voice, text, gain_db, noise_dbfs):
    """A 16 kHz mono WAV of `text`, at `gain_db`, over a pink noise floor when asked."""
    with tempfile.TemporaryDirectory(prefix="being-vt-") as folder:
        aiff = Path(folder) / "speech.aiff"
        subprocess.run(["say", "-v", voice, "-r", "145", "-o", str(aiff), text], check=True, timeout=30)
        shape = f"adelay=250,apad=pad_dur=1,volume={gain_db}dB"
        command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(aiff)]
        if noise_dbfs is None:
            command += ["-ac", "1", "-ar", "16000", "-af", shape, str(path)]
        else:
            amplitude = 10 ** (noise_dbfs / 20)
            command += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=16000:amplitude={amplitude:.6f}",
                        "-filter_complex", f"[0:a]aresample=16000,{shape}[s];[s][1:a]amix=inputs=2:duration=first:"
                        "normalize=0[m]", "-map", "[m]", "-ac", "1", "-ar", "16000", str(path)]
        subprocess.run(command, check=True, timeout=30)


def skip_reason():
    if not (shutil.which("say") and shutil.which("ffmpeg")):
        return "needs macOS say and ffmpeg"
    return fresh_image()


SKIP = skip_reason()


@unittest.skipIf(SKIP, SKIP or "")
class VoiceOpensTerminal(AudioHarness):
    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="being-vt-", dir="/tmp")))
        media = {}
        for name, voice, text, gain, noise in (*ONE_BREATH, COMMAND_ONLY):
            make_fixture(scratch / f"{name}.wav", voice, text, gain, noise)
            media[f"{name}.wav"] = scratch / f"{name}.wav"
        self.start_audio(media=media)
        self.sh("until systemctl is-active -q being-hands.service; do sleep 1; done", 180)
        self.vm.put("/run/app-watch.py", APP_WATCH)
        self.vm.put("/run/bus-send.py", BUS_SEND)
        self.vm.put("/run/frame.py", FRAME_PROBE)
        self.sh("(python3 /run/app-watch.py >/run/app-events 2>/run/app-watch.err &)")
        self.wait("pidof -x app-watch.py || pgrep -f app-watch.py", bool)
        # The model's warm-up competes for the CPU and talks to the model, so the
        # checks start after it: a command must then reach no model at all.
        self.wait("journalctl -b -u being-mind --no-pager -o cat", lambda log: "verified prompt tokens" in log,
                  timeout=330)
        self.results = []

    def tearDown(self):
        for line in self.results:
            print("  " + line, flush=True)

    # Helpers ---------------------------------------------------------------------------

    def app_events(self):
        events = []
        for line in self.sh("cat /run/app-events").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        return events

    def journal(self, unit, since):
        return self.sh(f"journalctl -u {unit} --no-pager -o cat --since=@{since:.3f}", timeout=30)

    def guest_time(self):
        return float(self.sh("date +%s.%N"))

    def frame_flags(self, app="terminal"):
        status, output = self.vm.run(f"python3 /run/frame.py {FRAME_DIR}/{app}.frame", 15)
        if status != 0:
            return None
        magic, flags = output.split()
        return int(flags) if magic == "BPN1" else None

    def rest(self):
        """Close the terminal if it is open and wait for Pip to settle at rest."""
        if any(e.get("app") == "terminal" and e.get("view") in ("shown", "minimised") for e in self.app_events()[-1:]):
            request = json.dumps({"t": "app.close", "app": "terminal", "req": f"vt{time.monotonic_ns() % 10**9}"})
            self.sh(f"python3 /run/bus-send.py {shlex.quote(request)}")
            self.wait("tail -1 /run/app-events", lambda out: '"view": "closed"' in out, timeout=30)
        deadline = time.monotonic() + 60
        while [e["state"] for e in self.events() if e["t"] == "state.changed"][-1:] != ["idle"]:
            self.assertLess(time.monotonic(), deadline, f"Pip did not come back to rest:\n{self.sh(f'tail -5 {EVENTS}')}")
            time.sleep(0.5)

    def opens_terminal(self, fixture, *, wake_first=False):
        self.rest()
        mark = len(self.events())
        apps = len(self.app_events())
        since = self.guest_time()
        self.sh("(arecord -q -D hw:Loopback,1,1 -f S16_LE -r 16000 -c 1 -d 25 /run/reply.wav "
                ">/run/reply.log 2>&1 &)")
        if wake_first:
            self.play("pip")
            self.event("hear.wake", after=mark)
            self.event("state.changed", after=mark, state="attending")
            self.play(fixture)
        else:
            self.play(fixture)
            self.event("hear.wake", after=mark, oneshot=True)
        final = self.event("hear.final", after=mark, timeout=60)
        voice_log = self.journal("being-voice", since)
        grammar = "grammar" if "second pass: matched" in voice_log else "n-gram"
        self.results.append(f"{fixture}: heard {final['text']!r} ({grammar} pass)")
        self.assertIn("terminal", final["text"], f"{fixture}: the words did not name the terminal")
        shown = self.wait("cat /run/app-events", lambda out: any(
            e.get("t") == "app.state" and e.get("app") == "terminal" and e.get("view") == "shown"
            for e in (json.loads(line) for line in out.splitlines()[apps:] if line.startswith("{"))), timeout=60)
        self.assertTrue(shown)
        mind_log = self.wait(f"journalctl -u being-mind --no-pager -o cat --since=@{since:.3f}",
                             lambda log: 'route {"route": ' in log, timeout=30)
        # The mind's lines read 'mind: route {...}' in the journal.
        routes = [match[1] for match in map(ROUTE.search, mind_log.splitlines()) if match]
        self.assertTrue(routes, f"{fixture}: no route line in the mind's log")
        self.assertIn('"route": "workshop.open"', routes[-1], f"{fixture}: routed as {routes[-1]}")
        if "launch_slot_" in self.journal("being-cortex", since):
            self.results.append(f"{fixture}: note: the model was asked something during this command")
        self.sh("systemctl is-active -q being-panel-terminal.service")
        deadline = time.monotonic() + 10
        while not (self.frame_flags() or 0) & VISIBLE:
            self.assertLess(time.monotonic(), deadline, f"{fixture}: the terminal's frame never became visible")
            time.sleep(0.25)
        self.event("say.done", after=mark, timeout=120, final=True)
        said = " ".join(e["text"] for e in self.events()[mark:] if e["t"] == "say.request").strip()
        self.assertEqual(said, REPLY)
        self.wait("! pgrep -f '^arecord -q -D hw:Loopback,1,1'", lambda _: True, timeout=40)
        status, heard_back = self.vm.run("command -v pocketsphinx >/dev/null && "
                                         "pocketsphinx single /run/reply.wav 2>/dev/null | tail -1", 120)
        if status == 0:
            self.results.append(f"{fixture}: Pip said {said!r}; PocketSphinx heard it back as {heard_back.strip()}")

    # Tests -------------------------------------------------------------------------------

    def test_one_breath_at_several_levels(self):
        failures = []
        for name, voice, _, gain, noise in ONE_BREATH:
            with self.subTest(fixture=name, voice=voice, gain_db=gain, noise_dbfs=noise):
                try:
                    self.opens_terminal(name)
                except AssertionError as error:
                    failures.append(f"{name}: {str(error).splitlines()[0]}")
                    raise
        if failures:
            self.results.append("failed: " + "; ".join(failures))

    def test_wake_then_the_command(self):
        self.opens_terminal(COMMAND_ONLY[0], wake_first=True)


if __name__ == "__main__":
    unittest.main()

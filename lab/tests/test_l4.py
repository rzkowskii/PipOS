"""Exercise Pip's real capture and playback paths with ALSA loopback."""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path

from lab.vm import IMAGE, VM

EVENTS = "/run/l4-events.jsonl"


class AudioHarness(unittest.TestCase):
    def sh(self, command, timeout=30):
        status, out = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{out}")
        return out

    def wait(self, command, check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, out = self.vm.run(command, 10)
            if status == 0 and check(out):
                return out
            time.sleep(0.1)
        self.fail(f"timed out waiting for {command}:\n{out[-2000:]}")

    def events(self):
        return [json.loads(line) for line in self.sh(f"cat {EVENTS}").splitlines()]

    def event(self, topic, after=0, timeout=30, **fields):
        def matching(out):
            for line in out.splitlines()[after:]:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue  # A concurrent append may end with a partial line.
                if value["t"] == topic and all(value.get(key) == wanted for key, wanted in fields.items()):
                    return value
            return None

        try:
            return matching(self.wait(f"cat {EVENTS}", matching, timeout))
        except AssertionError as error:
            journal = self.sh("journalctl -b -u being-voice --no-pager -o cat | tail -30")
            raise AssertionError(f"{error}\nVoice journal:\n{journal}") from error

    def key(self, name):
        self.vm.qmp("send-key", keys=[{"type": "qcode", "data": name}])

    def play(self, name, background=False):
        command = f"aplay -q -D plughw:Loopback,0,0 /mnt/updates/{name}.wav"
        self.sh(f"({command} >/run/input.log 2>&1 &)" if background else command)

    def start_audio(self, microphone_port=False, *, image=IMAGE, media=None):
        fixture = tempfile.TemporaryDirectory(prefix="being-audio-", dir="/tmp")
        self.addCleanup(fixture.cleanup)
        fixtures = Path(fixture.name)
        for name, voice, text, rate in (
            ("pip", "Samantha", "Hey Pip.", "140"),
            ("time", "Samantha", "What time is it?", "145"),
            ("hello", "Samantha", "Hello there. How are you today?", "145"),
            ("sea", "Samantha", "Tell me about the sea.", "145"),
            ("pip-karen", "Karen", "Hey Pip.", "145"),
            ("pick-a-number", "Karen", "Hey, pick a number.", "150"),
        ):
            source = fixtures / "source.aiff"
            subprocess.run(["say", "-v", voice, "-r", rate, "-o", str(source), text],
                           check=True, timeout=30)
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "16000",
                 "-af", "adelay=250,apad=pad_dur=1", str(fixtures / f"{name}.wav")],
                check=True, timeout=30,
            )
            source.unlink()

        self.fixtures = fixtures
        for name, source in (media or {}).items():
            shutil.copyfile(source, fixtures / name)
        self.vm = VM(image=image, loopback=True, audio_fixtures=fixtures, microphone_port=microphone_port)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        if microphone_port:
            microphone = self.vm._connect(self.vm.microphone_path)
            microphone.settimeout(12)
            self.addCleanup(microphone.close)
            self.microphone = microphone
            self.control = microphone.makefile("rb")
            self.addCleanup(self.control.close)
        self.vm.login(120)
        self.sh("mount -t vfat -o ro /dev/vda1 /mnt/updates")
        self.vm.put("/run/audio-events.py", """
import json, socket, time
bus = socket.socket(socket.AF_UNIX)
bus.connect('/run/being/bus.sock')
bus.sendall(b'{"t":"hello","name":"audio-test","sub":["app.privacy","organ.up","state.changed","hear.*","say.*","voice.mute"]}\\n')
positive = False
for line in bus.makefile():
    event = json.loads(line)
    if event['t'] == 'say.started':
        positive = False
    if event['t'] == 'say.level':
        if positive or event['v'] <= 0.02:
            continue
        positive = True
    event['monotonic'] = time.monotonic()
    print(json.dumps(event), flush=True)
""")
        self.sh(f"(python3 /run/audio-events.py >{EVENTS} 2>/run/audio-events.err &)")
        self.event("state.changed", state="idle", unwell=False)
        if not microphone_port:
            self.wait("pidof arecord", bool)

    def prepare_speaker_capture(self):
        self.vm.put("/run/first-speaker-audio.py", """
import array, subprocess, time

command = ["arecord", "-q", "-D", "hw:Loopback,1,1", "-t", "raw", "-f", "S16_LE",
           "-r", "16000", "-c", "1", "--period-time=30000", "--buffer-time=120000", "-d", "165"]
with subprocess.Popen(command, stdout=subprocess.PIPE) as capture:
    while frame := capture.stdout.read(960):
        if sum(abs(sample) > 500 for sample in array.array("h", frame)) >= 5:
            print(time.monotonic(), flush=True)
            capture.terminate()
            break
""")

    def measure_reply(self, fixture):
        self.sh("(python3 /run/first-speaker-audio.py >/run/first-speaker-audio 2>/run/speaker-capture.err &)")
        mark = len(self.events())
        self.play(fixture)
        final = self.event("hear.final", after=mark)
        self.event("say.started", after=mark, timeout=420)
        self.event("say.level", after=mark)
        self.event("say.done", after=mark, timeout=160, final=True)
        audio_at = float(self.wait("cat /run/first-speaker-audio", bool, timeout=10))
        first_audio = audio_at - final["monotonic"]
        self.assertGreater(first_audio, 0.0)
        reply = " ".join(e["text"] for e in self.events()[mark:] if e["t"] == "say.request").strip()
        return final["text"], reply, first_audio


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
@unittest.skipUnless(shutil.which("say") and shutil.which("ffmpeg"), "needs macOS say and ffmpeg")
class Audio(AudioHarness):
    def test_restarted_voice_keeps_guest_transcripts_private_with_debug_enabled(self):
        self.start_audio()
        # The lab bus supplies only the exact privacy command. The canary
        # below still enters through the restarted Voice's ALSA input.
        self.vm.put("/run/enter-audio-guest.py", """\
import json
import socket

with socket.socket(socket.AF_UNIX) as bus:
    bus.connect('/run/being/bus.sock')
    for event in ({'t': 'hello', 'name': 'audio-guest-fixture', 'sub': []}, {'t': 'hear.wake'},
                  {'t': 'hear.speech_start'}, {'t': 'hear.final', 'text': 'Start guest mode'}):
        bus.sendall((json.dumps(event) + '\\n').encode())
""")
        mark = len(self.events())
        self.sh("python3 /run/enter-audio-guest.py")
        self.event("app.privacy", after=mark, guest=True)
        self.event("say.done", after=mark, final=True)
        self.key("m")
        self.event("voice.mute", after=mark, on=True)

        dropin = "/run/systemd/system/being-voice.service.d"
        self.sh(f"mkdir -p {dropin}")
        self.vm.put(f"{dropin}/privacy-fixture.conf", "[Service]\nEnvironment=BEING_VOICE_DEBUG=1\n")
        old_pid = self.sh("systemctl show -p MainPID --value being-voice")
        mark = len(self.events())
        self.sh("systemctl daemon-reload && systemctl restart being-voice", timeout=60)
        self.event("organ.up", after=mark, name="voice")
        self.event("app.privacy", after=mark, guest=True)
        pid = self.sh("systemctl show -p MainPID --value being-voice")
        self.assertRegex(pid, r"^[1-9][0-9]*$")
        self.assertNotEqual(pid, old_pid)
        journal = f"journalctl -b -u being-voice _PID={pid} --no-pager -o cat"
        self.wait(journal, lambda output: "transcript text in the log on" in output)
        self.sh("! pidof arecord")

        self.key("m")
        self.event("voice.mute", after=mark, on=False)
        self.wait("pidof arecord", bool)
        self.play("pip")
        self.event("state.changed", after=mark, state="attending")
        self.play("time")
        self.event("hear.final", after=mark, text="what time is it")
        self.wait(journal, lambda output: "final transcript of" in output)
        self.assertNotIn("what time is it", self.sh(journal).lower())
        self.assertNotIn("what time is it", self.sh("journalctl -b --no-pager -o cat").lower())

    def test_pip_hears_decodes_speaks_and_honors_keyboard_controls(self):
        self.start_audio()
        self.play("time")
        self.assertFalse(any(e["t"].startswith("hear.") for e in self.events()))

        mark = len(self.events())
        self.key("m")
        self.event("voice.mute", after=mark, on=True)
        self.wait("! pidof arecord", lambda _: True)
        self.play("pip")
        self.assertFalse(any(e["t"].startswith("hear.") for e in self.events()[mark:]))

        self.key("m")
        self.event("voice.mute", after=mark, on=False)
        self.wait("pidof arecord", bool)
        self.play("pip")
        self.event("hear.wake", after=mark)
        self.event("state.changed", after=mark, state="attending")

        self.sh("(arecord -q -D hw:Loopback,1,1 -f S16_LE -r 16000 -c 1 -d 7 /run/reply.wav "
                ">/run/output.log 2>&1 &)")
        mark = len(self.events())
        self.play("time")
        self.event("hear.final", after=mark, text="what time is it")
        self.event("say.done", after=mark, final=True)
        topics = [e["t"] for e in self.events()[mark:]]
        self.assertLess(topics.index("hear.partial"), topics.index("hear.final"))
        self.assertLess(topics.index("say.started"), topics.index("say.done"))
        self.event("say.level", after=mark)
        samples = self.sh(
            """python3 -c 'import array,wave; w=wave.open("/run/reply.wav"); """
            """a=array.array("h", w.readframes(w.getnframes())); print(sum(abs(s)>500 for s in a))'"""
        )
        self.assertGreater(int(samples), 100, "the emulated speaker did not play synthesized PCM")

        mark = len(self.events())
        self.play("time", background=True)
        self.event("say.started", after=mark)
        self.key("esc")
        self.event("say.stop", after=mark)
        self.event("state.changed", after=mark, state="attending")
        self.wait("! pgrep -f '^aplay .*Loopback,0,1'", lambda _: True)
        self.assertFalse(any(e["t"] == "say.done" and e["final"] for e in self.events()[mark:]))

    def test_host_microphone_port_requests_audio_only_when_open(self):
        self.start_audio(microphone_port=True)
        self.assertEqual(self.control.readline(), b"start\n")
        mark = len(self.events())
        self.key("m")
        self.event("voice.mute", after=mark, on=True)
        self.assertEqual(self.control.readline(), b"stop\n")
        self.key("m")
        self.event("voice.mute", after=mark, on=False)
        self.assertEqual(self.control.readline(), b"start\n")

        for name in ("pick-a-number", "pip-karen", "time"):
            with wave.open(str(self.fixtures / f"{name}.wav")) as wav:
                while frame := wav.readframes(480):
                    self.microphone.sendall(frame)
                    time.sleep(0.03)
            if name == "pick-a-number":
                self.assertFalse(any(e["t"].startswith("hear.") for e in self.events()[mark:]))
            elif name == "pip-karen":
                self.event("hear.wake", after=mark)
                self.event("state.changed", after=mark, state="attending")
        self.event("hear.final", after=mark, text="what time is it")
        self.assertEqual(self.control.readline(), b"stop\n")
        self.event("say.done", after=mark, final=True)
        self.assertEqual(self.control.readline(), b"start\n")

    def test_a_greeting_and_a_real_model_reply_are_spoken_from_audio(self):
        self.start_audio()
        self.prepare_speaker_capture()
        self.play("pip")
        self.event("hear.wake")
        self.event("state.changed", state="attending")
        greeting, reply, latency = self.measure_reply("hello")
        self.assertEqual(greeting, "hello there how are you today")
        self.assertEqual(reply, "I'm doing well, thanks. How about you?")
        print(f"  greeted in {latency:.2f}s from final transcript to first ALSA PCM: {reply!r}", flush=True)

        journal = self.wait(
            "journalctl -b -u being-mind --no-pager -o cat",
            lambda log: "verified prompt tokens" in log,
            timeout=330,
        )
        warmup = next(line for line in journal.splitlines() if "verified prompt tokens" in line)
        mark = len(self.events())
        self.wait(f"tail -8 {EVENTS}", lambda out: '"state": "idle"' in out, timeout=40)
        self.play("pip")
        self.event("hear.wake", after=mark)
        self.event("state.changed", after=mark, state="attending")
        text, reply, latency = self.measure_reply("sea")
        self.assertIn("tell me about", text)
        self.assertGreater(len(reply.split()), 1)
        self.assertNotIn("lost my thought", reply)
        self.assertNotIn("can't think right now", reply)
        print(f"  {warmup}; {latency:.2f}s from final transcript to first ALSA playback PCM", flush=True)
        print(f"  heard {text!r} and spoke {reply!r}", flush=True)

"""Trace a prerecorded turn from input endpoint to speaker PCM in QEMU."""

import argparse
import hashlib
import json
import shlex
import shutil
import signal
import textwrap
import unittest
from pathlib import Path

from lab.tests.test_l4 import EVENTS, AudioHarness
from lab.vm import IMAGE


CASES = {"time": "what time is it", "sea": "tell me about the sea"}
STUB_CLAUSES = ("The sea is wide.", "Waves roll toward the shore.")
STUB_REPLY = " ".join(STUB_CLAUSES)
STUB_STREAM = f"[neutral] {STUB_REPLY} (Private note: never say this.)"
VOICE_MODULE = "/usr/lib/being/mind/voice.py"
BASELINE_MEDIA = "voice-baseline.py"
FIRST_PAUSE_ENV = "BEING_VOICE_SHORTEN_FIRST_PAUSE"
FIXTURE_TIMING = textwrap.dedent("""\
    import array
    import json
    import re
    import subprocess
    import sys
    import time
    import wave
    from pathlib import Path

    status = Path("/proc/asound/Loopback/pcm0p/sub0/status")
    path = sys.argv[1]
    threshold = 160
    with wave.open(path) as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000):
            raise ValueError("expected 16 kHz mono S16_LE fixture")
        samples = array.array("h", wav.readframes(wav.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    endpoint = next((i + 1 for i in range(len(samples) - 1, -1, -1)
                     if abs(samples[i]) >= threshold), None)
    if endpoint is None or not status.is_file():
        raise RuntimeError("fixture has no audible frames or playback status is unavailable")

    args = ["aplay", "-q", "-D", "plughw:Loopback,0,0",
            "--period-time=30000", "--buffer-time=120000", path]
    with subprocess.Popen(args) as player:
        try:
            previous = None
            deadline = time.monotonic() + 12
            while player.poll() is None and time.monotonic() < deadline:
                before = time.monotonic()
                fields = dict(re.findall(r"^(\\w+)\\s*:\\s*([^\\n]+)", status.read_text(), re.M))
                observed = (before + time.monotonic()) / 2
                if fields.get("owner_pid") == str(player.pid) and fields.get("state") == "RUNNING":
                    played = int(fields["hw_ptr"])
                    if played >= endpoint:
                        if previous is None:
                            raise RuntimeError("first ALSA observation was after the fixture endpoint")
                        earlier, earlier_frame = previous
                        fraction = (endpoint - earlier_frame) / (played - earlier_frame)
                        stamp = earlier + fraction * (observed - earlier)
                        player.wait(timeout=5)
                        if player.returncode:
                            raise RuntimeError(f"input aplay exited {player.returncode}")
                        print(json.dumps({"end_monotonic": stamp, "threshold_s16": threshold,
                                          "fixture_end_frame": endpoint, "fixture_frames": len(samples),
                                          "endpoint_bracket_ms": round(1000 * (observed - earlier), 2)}))
                        break
                    previous = observed, played
                time.sleep(0.01)
            else:
                raise RuntimeError("did not observe the fixture endpoint in ALSA playback")
        finally:
            if player.poll() is None:
                player.kill()
""")
CAPTURE_AND_GAPS = textwrap.dedent("""\
    import array
    import json
    import subprocess
    import time
    from pathlib import Path

    command = ["arecord", "-q", "-D", "hw:Loopback,1,1", "-t", "raw", "-f", "S16_LE",
               "-r", "16000", "-c", "1", "--period-time=30000", "--buffer-time=120000", "-d", "30"]
    stop = Path("/run/voice-latency-stop")
    block_number = 0
    first = last = None
    gaps = []
    with subprocess.Popen(command, stdout=subprocess.PIPE) as capture:
        with Path("/run/voice-latency.pcm").open("wb") as audio:
            while block := capture.stdout.read(320):
                audio.write(block)
                loud = sum(abs(sample) > 500 for sample in array.array("h", block)) >= 5
                if loud:
                    if first is None:
                        first = block_number
                        Path("/run/first-speaker-audio").write_text(str(time.monotonic()))
                    elif block_number - last > 5:
                        gaps.append({"start_ms": (last + 1 - first) * 10,
                                     "duration_ms": (block_number - last - 1) * 10})
                    last = block_number
                block_number += 1
                if stop.exists() and last is not None and block_number - last >= 15:
                    capture.terminate()
                    break
    if first is None:
        raise RuntimeError("no audible output PCM")
    Path("/run/voice-latency-pcm.json").write_text(json.dumps({
        "block_ms": 10, "sample_threshold_s16": 500, "minimum_samples_per_block": 5,
        "captured_ms": block_number * 10, "audible_span_ms": (last + 1 - first) * 10,
        "internal_silence": gaps,
        "longest_internal_silence_ms": max((gap["duration_ms"] for gap in gaps), default=0),
    }))
""")


class VoiceLatency(AudioHarness):
    def __init__(self, image, case, baseline, repeats, *, first_pause_trial=False):
        super().__init__()
        self.image = image
        self.case = case
        self.baseline = baseline
        self.repeats = repeats
        self.first_pause_trial = first_pause_trial

    def play(self, name, background=False):
        if name != self.case or background:
            return super().play(name, background)
        command = shlex.join(["python3", "/run/voice-fixture-end.py", f"/mnt/updates/{name}.wav"])
        self.fixture_timing = json.loads(self.sh(command))

    def prepare_speaker_capture(self):
        if self.case == "sea":
            self.vm.put("/run/first-speaker-audio.py", CAPTURE_AND_GAPS)
        else:
            super().prepare_speaker_capture()

    def replace_cortex(self):
        # Leave recognition, the event bus, mind and voice in the installed system.
        self.sh("systemctl stop being-mind being-cortex", timeout=90)
        source = Path(__file__).resolve().parent.parent / "sim" / "fake_cortex.py"
        script = source.read_text().split('if __name__ == "__main__":', 1)[0]
        script += f"\nFakeCortex(port=8080, delay=0, reply=lambda _: {STUB_STREAM!r}).server.serve_forever()\n"
        self.vm.put("/run/voice-latency-cortex.py", script)
        self.sh("mkdir -p /run/systemd/system/being-cortex.service.d")
        self.vm.put("/run/systemd/system/being-cortex.service.d/latency.conf", """[Service]
ExecStart=
ExecStart=/usr/bin/python3 /run/voice-latency-cortex.py
""")
        self.sh("systemctl daemon-reload && systemctl start being-cortex being-mind")
        self.wait(
            'pid=$(systemctl show -p MainPID --value being-mind); test "$pid" -gt 0 && '
            'journalctl -b -u being-mind _PID="$pid" --no-pager -o cat',
            lambda out: "cortex is answering" in out,
        )

    def set_first_pause(self, enabled):
        directory = "/run/systemd/system/being-voice.service.d"
        value = f"{FIRST_PAUSE_ENV}={int(enabled)}"
        self.sh(f"mkdir -p {directory}")
        self.vm.put(f"{directory}/80-first-pause-trial.conf", f"[Service]\nEnvironment={value}\n")
        self.sh("systemctl daemon-reload && systemctl restart being-voice")
        self.wait(
            'pid=$(systemctl show -p MainPID --value being-voice); test "$pid" -gt 0 && '
            f'tr \'\\000\' \'\\n\' </proc/"$pid"/environ | grep -Fx {shlex.quote(value)}',
            bool,
        )

    def measure_turn(self):
        status = "/proc/asound/Loopback/pcm0p/sub0/status"
        self.sh(f"test -r {status}")
        self.sh("rm -f /run/voice-latency-stop /run/voice-latency-pcm.json /run/voice-latency.pcm "
                "/run/first-speaker-audio")
        resting = self.wait(
            f"grep 'state.changed' {EVENTS} | tail -1",
            lambda out: any(f'"state": "{state}"' in out for state in ("idle", "attending")),
        )
        self.wait("for pid in $(pidof arecord); do "
                  "tr '\\000' ' ' </proc/$pid/cmdline 2>/dev/null | grep -F 'hw:Loopback,1,0' && break; done", bool)
        mark = len(self.events())
        if json.loads(resting)["state"] == "idle":
            self.play("pip")
            self.event("hear.wake", after=mark)
            self.event("state.changed", after=mark, state="attending")
        text, reply, final_to_pcm = self.measure_reply(self.case)
        final = self.event("hear.final", after=mark)["monotonic"]
        started = self.event("say.started", after=mark)["monotonic"]
        requests = [event for event in self.events()[mark:] if event["t"] == "say.request" and event["text"]]
        clause = requests[0]["monotonic"]
        endpoint = self.fixture_timing["end_monotonic"]
        self.assertEqual(text, CASES[self.case])
        if self.case == "time":
            self.assertTrue(reply.startswith("It's "), reply)
        else:
            self.assertEqual(reply, STUB_REPLY)
            self.assertEqual(tuple(request["text"] for request in requests), STUB_CLAUSES)
        self.assertLess(endpoint, final, "final transcript preceded the fixture endpoint")
        self.assertLessEqual(final, clause)
        self.assertLessEqual(clause, started)
        self.assertLessEqual(clause, final + final_to_pcm)
        result = {
            "case": self.case,
            "route": "local clock" if self.case == "time" else "scripted Cortex",
            "heard": text,
            "spoken": reply,
            "fixture": self.fixture_timing,
            "end_to_final_ms": round(1000 * (final - endpoint), 2),
            "final_to_first_clause_ms": round(1000 * (clause - final), 2),
            "first_clause_to_pcm_ms": round(1000 * (final + final_to_pcm - clause), 2),
            "first_clause_to_say_started_ms": round(1000 * (started - clause), 2),
            "say_started_to_pcm_ms": round(1000 * (final + final_to_pcm - started), 2),
            "end_to_pcm_ms": round(1000 * (final + final_to_pcm - endpoint), 2),
        }
        if self.case == "sea":
            self.sh("touch /run/voice-latency-stop")
            pcm = json.loads(self.wait("cat /run/voice-latency-pcm.json", bool, timeout=15))
            result["pcm"] = pcm
        return result

    def runTest(self):
        media = {BASELINE_MEDIA: self.baseline} if self.baseline else None
        self.start_audio(image=self.image, media=media)
        if self.case == "sea":
            self.replace_cortex()
        self.vm.put("/run/voice-fixture-end.py", FIXTURE_TIMING)
        self.prepare_speaker_capture()
        digest = self.sh(f"sha256sum {VOICE_MODULE}").split()[0]

        variants = [("candidate", digest)]
        active = "candidate"
        if self.baseline:
            baseline_digest = hashlib.sha256(self.baseline.read_bytes()).hexdigest()
            self.assertNotEqual(baseline_digest, digest, "baseline and packaged voice are identical")
            variants.insert(0, ("baseline", baseline_digest))
        elif self.first_pause_trial:
            self.sh(f"grep -Fq {FIRST_PAUSE_ENV} {VOICE_MODULE}")
            variants.insert(0, ("baseline", digest))
            active = None
        for repeat in range(1, self.repeats + 1):
            # Reverse every other pair to reduce bias from drift during the boot.
            order = variants if repeat % 2 else reversed(variants)
            for label, expected_digest in order:
                if label != active:
                    if self.first_pause_trial:
                        self.set_first_pause(label == "candidate")
                    else:
                        change = (f"mount -o bind /mnt/updates/{BASELINE_MEDIA} {VOICE_MODULE}"
                                  if label == "baseline" else f"umount {VOICE_MODULE}")
                        self.sh(f"systemctl stop being-voice && {change} && systemctl start being-voice")
                    active = label
                self.assertEqual(self.sh(f"sha256sum {VOICE_MODULE}").split()[0], expected_digest)
                result = self.measure_turn()
                if self.first_pause_trial:
                    result["first_pause_trial"] = label == "candidate"
                print(json.dumps({"variant": label, "voice_sha256": expected_digest,
                                  "repeat": repeat, **result}, indent=2), flush=True)


def expired(signum, frame):
    raise TimeoutError("voice latency guest budget expired")


def terminated(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=IMAGE)
    parser.add_argument("--case", choices=CASES, default="time")
    comparison = parser.add_mutually_exclusive_group()
    comparison.add_argument("--baseline-voice", type=Path, help="compare with this voice.py in the same guest")
    comparison.add_argument("--first-pause-trial", action="store_true",
                            help="alternate the packaged first-clause pause trial off and on")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--budget", type=int, default=360, help="host seconds, including boot")
    args = parser.parse_args()
    if not args.image.is_file() or args.budget < 1:
        parser.error("the image must exist and the budget must be positive")
    if args.baseline_voice and not args.baseline_voice.is_file():
        parser.error("the baseline voice.py must exist")
    if not shutil.which("say") or not shutil.which("ffmpeg"):
        parser.error("the PCM fixtures require macOS say and ffmpeg")
    trial = VoiceLatency(args.image.resolve(), args.case, args.baseline_voice, args.repeats,
                         first_pause_trial=args.first_pause_trial)
    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, terminated)
    signal.setitimer(signal.ITIMER_REAL, args.budget)
    try:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([trial]))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        trial.doCleanups()
    return int(not result.wasSuccessful())


if __name__ == "__main__":
    raise SystemExit(main())

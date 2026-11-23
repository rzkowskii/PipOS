"""Replay matched audio against Qwen and an LFM2.5 candidate in disposable PipOS guests."""

import argparse
import hashlib
import json
import re
import shlex
import shutil
import signal
import sys
import tempfile
import textwrap
import time
import traceback
from pathlib import Path

from lab.tests.test_l4 import EVENTS, AudioHarness
from lab.vm import IMAGE


FIXTURE_NAME = "candidate.gguf"
LFM_SHA256 = "3d10b6ab8fc91a919534b9558e266255aca0bbc7f6d015963599aa9e74e05b1d"
LFM_BYTES = 219_312_832
CANDIDATE_PATH = f"/mind/{FIXTURE_NAME}"
LFM_TEMPERATURE = 0.1
LFM_TOP_K = 50
LFM_REPEAT_PENALTY = 1.05
CASES = {"sea": "tell me about the sea", "time": "what time is it"}
MODEL_JOURNAL = "journalctl -b -u being-cortex --no-pager -o cat"
GENERATION = re.compile(r"slot launch_slot_: .*processing task")


class VoiceAudition(AudioHarness):
    def __init__(self, image, candidate, record):
        super().__init__()
        self.image = image
        self.candidate = candidate
        self.record = record

    def install_candidate(self):
        source = f"/mnt/updates/{FIXTURE_NAME}"
        self.sh("systemctl stop being-mind being-cortex", timeout=110)
        # The factory mind can save a context while stopping. Start the candidate
        # with a fresh context and a separate slot in this disposable overlay.
        self.sh("rm -f /mind/context.json")
        self.sh(f"cp {source} {CANDIDATE_PATH}", timeout=90)
        self.vm.put("/run/audition-digest.py", textwrap.dedent("""\
            import hashlib
            import sys

            with open(sys.argv[1], "rb") as weights:
                print(hashlib.file_digest(weights, "sha256").hexdigest())
            """))
        digest = self.sh(f"python3 /run/audition-digest.py {CANDIDATE_PATH}", timeout=90)
        self.assertEqual(digest, LFM_SHA256, "guest candidate bytes differ from the pinned GGUF")
        self.record["guest_candidate_sha256"] = digest
        unit = self.sh("cat \"$(systemctl show -p FragmentPath --value being-cortex)\"")
        exec_start, = (line for line in unit.replace("\\\n", " ").splitlines() if line.startswith("ExecStart="))
        argv = shlex.split(exec_start.removeprefix("ExecStart="))
        argv[argv.index("--model") + 1] = CANDIDATE_PATH
        argv[argv.index("--slot-save-path") + 1] = "/mind/audition-slot"
        argv.extend(["--top-k", str(LFM_TOP_K), "--repeat-penalty", str(LFM_REPEAT_PENALTY)])
        self.sh("mkdir -p /mind/audition-slot /run/systemd/system/being-cortex.service.d")
        self.vm.put("/run/systemd/system/being-cortex.service.d/audition.conf",
                    f"[Service]\nExecStart=\nExecStart={shlex.join(argv)}\n")
        config = json.loads(self.sh("cat /etc/being/being.json"))
        config["temperature"] = LFM_TEMPERATURE
        self.vm.put("/run/audition-voice-settings.json", json.dumps(config))
        self.sh("mount -o bind /run/audition-voice-settings.json /etc/being/being.json && "
                "systemctl daemon-reload && systemctl start being-cortex being-mind")

    def record_server(self):
        self.vm.put("/run/audition-props.py", textwrap.dedent("""\
            import json
            from urllib.request import urlopen

            with urlopen("http://127.0.0.1:8080/props", timeout=5) as response:
                props = json.load(response)
            defaults = props["default_generation_settings"]["params"]
            print(json.dumps({
                "build_info": props["build_info"],
                "model_path": props["model_path"],
                "model_alias": props["model_alias"],
                "total_slots": props["total_slots"],
                "ctx_tokens": props["default_generation_settings"]["n_ctx"],
                "top_k": defaults["top_k"],
                "repeat_penalty": defaults["repeat_penalty"],
            }))
            """))
        server = json.loads(self.wait("python3 /run/audition-props.py", bool, timeout=60))
        config = json.loads(self.sh("cat /etc/being/being.json"))
        server["temperature"] = config["temperature"]
        self.record["server"] = server
        path = CANDIDATE_PATH if self.candidate else "/models/reflex.gguf"
        self.assertEqual(server["model_path"], path)
        self.assertEqual(server["total_slots"], 1)
        self.assertEqual(server["ctx_tokens"], config["ctx_tokens"])
        if self.candidate:
            self.assertEqual(server["temperature"], LFM_TEMPERATURE)
            self.assertEqual(server["top_k"], LFM_TOP_K)
            self.assertAlmostEqual(server["repeat_penalty"], LFM_REPEAT_PENALTY, places=5)

    def journal_cursor(self):
        journal = self.sh(f"{MODEL_JOURNAL} --show-cursor -n 1")
        cursor = re.search(r"^-- cursor: (\S+)$", journal, re.MULTILINE)
        self.assertIsNotNone(cursor, journal)
        return cursor[1]

    def runTest(self):
        started = time.monotonic()
        media = {FIXTURE_NAME: self.candidate} if self.candidate else None
        self.start_audio(image=self.image, media=media)
        if self.candidate:
            self.install_candidate()
        self.record["server_version"] = self.sh("/usr/bin/llama-server --version 2>&1").splitlines()
        features = set(self.sh("grep -m 1 '^flags' /proc/cpuinfo").partition(":")[2].split())
        self.assertIn("avx", features)
        self.assertNotIn("avx2", features)
        self.prepare_speaker_capture()
        self.record_server()

        warmup = re.compile(r"warmed (\d+) verified prompt tokens")
        warm_journal = ('pid=$(systemctl show -p MainPID --value being-mind); test "$pid" -gt 0 && '
                        'journalctl -b -u being-mind _PID="$pid" --no-pager -o cat')
        try:
            journal = self.wait(warm_journal, warmup.search, timeout=180 if self.candidate else 330)
        except AssertionError:
            if not self.candidate:
                raise
            self.record["warm_status"] = "unverified"
            self.record["mind_warm_journal"] = self.sh(warm_journal).splitlines()
            self.record["cortex_warm_journal"] = self.sh(f"{MODEL_JOURNAL} -n 40").splitlines()
            print("  candidate warmup remained unverified; measuring voice compatibility", file=sys.stderr, flush=True)
        else:
            self.record["warm_status"] = "verified"
            self.record["verified_warm_tokens"] = int(warmup.search(journal)[1])
            self.record["harness_start_to_warm_s"] = round(time.monotonic() - started, 3)
            print(f"  verified {self.record['verified_warm_tokens']} warm tokens", file=sys.stderr, flush=True)

        for case, expected in CASES.items():
            self.wait(
                f"grep 'state.changed' {EVENTS} | tail -1",
                lambda out: '"state": "idle"' in out,
                timeout=55,
            )
            mark = len(self.events())
            self.play("pip")
            self.event("hear.wake", after=mark)
            self.event("state.changed", after=mark, state="attending")
            cursor = self.journal_cursor()
            text, reply, latency = self.measure_reply(case)
            events = self.events()[mark:]
            final = next(e for e in events if e["t"] == "hear.final")
            first_clause = next(e for e in events if e["t"] == "say.request" and e.get("text"))
            self.record["turns"][case] = {
                "transcript": text, "reply": reply,
                "first_clause": first_clause["text"],
                "first_clause_s": round(first_clause["monotonic"] - final["monotonic"], 3),
                "first_pcm_s": round(latency, 3),
            }
            print(f"  {text!r} → {reply!r} in {latency:.2f}s to PCM", file=sys.stderr, flush=True)
            self.assertEqual(text, expected)
            journal = self.sh(f"{MODEL_JOURNAL} --after-cursor {shlex.quote(cursor)}")
            if case == "time":
                self.assertTrue(reply.startswith("It's "), reply)
                self.assertIsNone(GENERATION.search(journal), journal)
            else:
                self.record["model_turn_journal"] = journal.splitlines()
                self.assertRegex(journal, GENERATION)
                self.assertNotIn("lost my thought", reply.lower())
                self.assertNotIn("can't think right now", reply.lower())


def expired(signum, frame):
    raise TimeoutError("guest audition budget expired")


def terminated(signum, frame):
    raise KeyboardInterrupt


def checkpoint(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as output:
            temporary = Path(output.name)
            json.dump(report, output, indent=2)
            output.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="official LFM2.5-350M QAD Q4_0 GGUF")
    parser.add_argument("--image", type=Path, default=IMAGE)
    parser.add_argument("--models", nargs="+", choices=("lfm", "qwen"), default=("lfm", "qwen"))
    parser.add_argument("--budget", type=int, default=600, help="host seconds per guest, including boot")
    parser.add_argument("--output", type=Path, help="also save an atomic report after each guest")
    args = parser.parse_args()
    if not args.image.is_file() or not args.candidate.is_file():
        parser.error("both the factory disk image and candidate GGUF must exist")
    if not shutil.which("say") or not shutil.which("ffmpeg"):
        parser.error("the WAV fixtures need macOS say and ffmpeg")
    if args.budget < 1:
        parser.error("the guest budget must be positive")

    image = args.image.resolve()
    candidate = args.candidate.resolve()
    if candidate.stat().st_size != LFM_BYTES:
        parser.error("candidate size differs from the pinned Liquid AI GGUF")
    with candidate.open("rb") as source:
        candidate_digest = hashlib.file_digest(source, "sha256").hexdigest()
    if candidate_digest != LFM_SHA256:
        parser.error(f"candidate SHA-256 differs from the pinned Liquid AI GGUF: {candidate_digest}")
    report = {
        "scope": "one prerecorded utterance per case and model, measured inside emulated Ivy Bridge guests",
        "interpretation": "exploratory voice compatibility and relative latency; physical timing unmeasured",
        "image": str(image),
        "image_mtime_ns": image.stat().st_mtime_ns,
        "candidate_sha256": candidate_digest,
        "candidate_sampling": {"temperature": LFM_TEMPERATURE, "top_k": LFM_TOP_K,
                               "repeat_penalty": LFM_REPEAT_PENALTY},
        "models": [],
        "complete": False,
    }
    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, terminated)
    for label in args.models:
        print(f"QEMU {label} voice audition", file=sys.stderr, flush=True)
        record = {"model": label, "turns": {}}
        report["models"].append(record)
        audition = VoiceAudition(image, candidate if label == "lfm" else None, record)
        signal.setitimer(signal.ITIMER_REAL, args.budget)
        try:
            audition.runTest()
        except Exception:
            error = traceback.format_exc()
            record["errors"] = [error]
            print(error, file=sys.stderr)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if not audition.doCleanups():
                error = f"QEMU {label} cleanup failed"
                record.setdefault("errors", []).append(error)
                print(error, file=sys.stderr)
        if args.output:
            checkpoint(args.output, report)
    report["complete"] = True
    if args.output:
        checkpoint(args.output, report)
    print(json.dumps(report, indent=2))
    return int(any("errors" in row for row in report["models"]))


if __name__ == "__main__":
    raise SystemExit(main())

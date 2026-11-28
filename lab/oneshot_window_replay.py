"""Replay a one-breath window through the real Silero and Moonshine (run on the laptop).

The host tests (tests/test_asr_oneshot_windows.py) use a stand-in for Silero
that judges a window by its level, and synthetic audio. They show the
decision logic, not that real Silero finds no speech in the noise after "Hey
Pip". This script shows that: it feeds `mind.asr_sherpa.SherpaRecognizer`, built
with the real models, the way a one-breath wake does, and prints what reached
Moonshine and what came back.

    PYTHONPATH=<code tree> python3 lab/oneshot_window_replay.py \
        --window name-window.wav --request request.wav --pause 1.5

`--window` is the audio after the name that the wake replays (a noise window
for "Hey Pip", pause, request; the whole command for one breath). `--request`
is spoken `--pause` seconds into the live audio (leave it out for none). Run
it once with PYTHONPATH on the fixed tree and once on the tree before the fix,
and compare the `sent` lists: the fixed code sends the request only; the old
code sends the noise window first. The WAVs are 16 kHz mono; nothing here is
kept or sent anywhere.
"""

import argparse
import json
import sys
import time
import wave
from array import array
from pathlib import Path

from mind import asr_sherpa
from mind.recognition import FRAME_BYTES, FRAME_SAMPLES, RecognizerEngine

FRAME_SECONDS = FRAME_SAMPLES / 16000


def read_frames(path):
    """A 16 kHz mono WAV as 30 ms frames of 16-bit PCM (the last one padded)."""
    with wave.open(str(path)) as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16 kHz mono 16-bit")
        samples = array("h", wav.readframes(wav.getnframes()))
    samples.extend([0] * (-len(samples) % FRAME_SAMPLES))
    data = samples.tobytes()
    return [data[i:i + FRAME_BYTES] for i in range(0, len(data), FRAME_BYTES)]


class OneBreathWake(RecognizerEngine):
    """Stands in for PocketSphinx: reports a one-breath wake at a chosen frame."""

    def __init__(self, at, replayed):
        self.at, self.replayed, self.count = at, replayed, 0
        self.oneshot_queue = ()

    def start_capture(self):
        pass

    def stop_capture(self):
        pass

    def close(self):
        pass

    def feed(self, frame, attending, oneshot=True):
        self.count += 1
        if self.count == self.at:
            self.oneshot_queue = [b""] * self.replayed
            return [("hear.wake", {"oneshot": True}), ("hear.speech_start", {"oneshot": True})]
        return []


def replay(recognizer, window, request=None, pause=1.5, tail=1.5, timeout=20.0):
    """Feed `window` while resting, then live silence, the request and a tail. Returns what happened.

    `recognizer.wake` must be an OneBreathWake. `sent` lists the audio lengths
    (seconds) that reached the recogniser, in order.
    """
    sent = []
    inner = recognizer.recognizer
    transcribe = inner.transcribe

    def spy(samples, rate=16000):
        sent.append(round(len(samples) / rate, 2))
        return transcribe(samples, rate)

    inner.transcribe = spy
    events = []
    silence = bytes(FRAME_BYTES)
    live = [silence] * round(pause / FRAME_SECONDS) + (request or []) + [silence] * round(tail / FRAME_SECONDS)
    started = time.monotonic()
    try:
        for frame in window:
            events.extend(recognizer.feed(frame, False))
        for frame in live:
            events.extend(recognizer.feed(frame, True))
        deadline = time.monotonic() + timeout
        while not any(topic == "hear.final" for topic, _ in events) and time.monotonic() < deadline:
            events.extend(recognizer.feed(silence, True))
            time.sleep(0.01)
    finally:
        inner.transcribe = transcribe
    finals = [fields.get("text") for topic, fields in events if topic == "hear.final"]
    return {"sent": sent, "finals": finals, "events": [topic for topic, _ in events],
            "seconds": round(time.monotonic() - started, 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--window", type=Path, required=True, help="audio after the name that the wake replays")
    parser.add_argument("--request", type=Path, help="the request, spoken --pause seconds after the wake")
    parser.add_argument("--pause", type=float, default=1.5)
    parser.add_argument("--root", type=Path, default=asr_sherpa.ROOT, help="the speech models (default /models/asr)")
    parser.add_argument("--size", default="tiny")
    args = parser.parse_args(argv)
    window = read_frames(args.window)
    request = read_frames(args.request) if args.request else None
    wake = OneBreathWake(len(window), len(window))
    recognizer = asr_sherpa.SherpaRecognizer.load(wake, root=args.root, size=args.size)
    try:
        result = replay(recognizer, window, request, args.pause)
    finally:
        recognizer.close()
    result["code"] = str(Path(asr_sherpa.__file__).resolve())
    json.dump(result, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()

"""Describe one approved camera frame using the optional signed vision pack."""

import argparse
import http.client
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from . import model_pack, vision_runtime
from .eyes import rgb_payload_size, valid_description


SERVER = Path("/usr/bin/llama-server")
LOOK_SECONDS = 25.0
PPM_TYPE = "image/x-portable-pixmap"


def read_rgb(stream):
    if stream.readline(4) != b"P6\n":
        raise ValueError("expected RGB P6")
    dimensions = stream.readline(9)
    size = rgb_payload_size(dimensions)
    if stream.readline(5) != b"255\n":
        raise ValueError("expected RGB samples with a maximum of 255")
    pixels = stream.read(size + 1)
    if len(pixels) != size:
        raise ValueError("expected exactly one complete RGB frame")
    return b"P6\n" + dimensions + b"255\n" + pixels


def signed_inputs():
    # Explicit descriptor paths also require the digest to be bound to the
    # system's own copy. The signature-only legacy fallback is insufficient.
    pack = model_pack.verify("vision", descriptor=model_pack.descriptor_path("vision"), provisional=False)
    base = model_pack.root("vision")
    weights = base / "vision.gguf"
    if pack["vision_weights"] == "conversation":
        conversation = model_pack.verify("conversation", descriptor=model_pack.DEFAULT_DESCRIPTOR, provisional=False)
        if conversation["sha256"] != pack["conversation_sha256"]:
            raise ValueError("vision projector was qualified with different conversation weights")
        weights = model_pack.root("conversation") / model_pack.WEIGHTS
    return weights, base / "mmproj.gguf", pack["vision_profile"]


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("vision deadline expired")
    return seconds


def describe(stream, deadline):
    frame = read_rgb(stream)
    model, projector, profile = signed_inputs()
    with tempfile.TemporaryDirectory(prefix="pv-", dir=os.environ.get("BEING_VISION_WORKDIR", "/tmp")) as directory:
        with vision_runtime.server(SERVER, model, projector, Path(directory), profile,
                                   timeout=remaining(deadline)) as (socket_path, _):
            answer, finish, _ = vision_runtime.describe(socket_path, frame, PPM_TYPE, timeout=remaining(deadline))
    if finish != "stop" or not valid_description(answer):
        raise ValueError("vision reply was incomplete or invalid")
    return answer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        line = describe(sys.stdin.buffer, time.monotonic() + LOOK_SECONDS)
    except (OSError, KeyError, ValueError, http.client.HTTPException, subprocess.SubprocessError):
        return 1
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    def interrupt(_signum, _frame):
        raise InterruptedError("vision interrupted")

    for event in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM, signal.SIGALRM):
        signal.signal(event, interrupt)
    signal.setitimer(signal.ITIMER_REAL, LOOK_SECONDS)
    try:
        sys.exit(main())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)

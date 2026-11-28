"""Compare local single-frame descriptions on the shipped llama-server interface."""

import argparse
from dataclasses import dataclass
import hashlib
import http.client
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile

from lab.candidate_manifest import fingerprint, publish, regular
from lab.hardware_image import artifact_identity
from mind import vision_runtime
from mind.eyes import valid_description


PHOTO_SHA256 = {
    "moonwalk-320.jpg": "b793baffa5feff832983eee160d126f398fac47cbf99976b002766f247d64bad",
    "shuttle-320.jpg": "be4ce97b5c8af343c5b8022bfa904261629bfafa0b5c866d8ed3e814d9f91ca8",
}


@dataclass(frozen=True)
class Frame:
    label: str
    image: bytes
    content_type: str
    terms: tuple[tuple[str, ...], ...] = ()
    other_subject: tuple[str, ...] = ()


def shapes(label):
    width, height = 320, 240
    data = bytearray(f"P6\n{width} {height}\n255\n".encode("ascii"))
    for y in range(height):
        for x in range(width):
            if label == "red-circle" and (x - 160)**2 + (y - 120)**2 <= 75**2:
                data.extend((226, 24, 32))
            elif label == "blue-triangle" and 35 <= y <= 195 and abs(x - 160) <= (y - 35) * 0.53:
                data.extend((24, 65, 223))
            else:
                data.extend((8, 8, 8) if label == "dark" else (239, 239, 239))
    return bytes(data)


def cases(photos):
    circle, triangle = ("circle", "circular"), ("triangle", "triangular")
    yield Frame("red-circle", shapes("red-circle"), "image/x-portable-pixmap", (("red",), circle),
                (*triangle, "blue"))
    yield Frame("blue-triangle", shapes("blue-triangle"), "image/x-portable-pixmap", (("blue",), triangle),
                (*circle, "red"))
    for label in ("blank", "dark"):
        yield Frame(label, shapes(label), "image/x-portable-pixmap")
    person = ("astronaut", "spacesuit", "space suit")
    vehicle = ("rocket", "shuttle", "spacecraft")
    for name, terms, other in (("moonwalk-320.jpg", person, vehicle), ("shuttle-320.jpg", vehicle, person)):
        with regular(photos / name) as (stream, info):
            if not 0 < info.st_size <= vision_runtime.MAX_FRAME_BYTES:
                raise ValueError(f"Invalid size for the pinned photo {name}.")
            data = stream.read(vision_runtime.MAX_FRAME_BYTES + 1)
        if hashlib.sha256(data).hexdigest() != PHOTO_SHA256[name]:
            raise ValueError(f"The photo does not match the pinned fixture: {name}.")
        yield Frame(name.removesuffix("-320.jpg"), data, "image/jpeg", (terms,), other)


def identify(path, expected=None):
    with regular(path) as (stream, info):
        if not info.st_size:
            raise ValueError(f"Empty audition artifact: {path.name}.")
        identity = fingerprint(stream, info.st_size)
    if expected is not None and identity["sha256"] != expected.lower():
        raise ValueError(f"Digest mismatch for {path.name}.")
    return {"name": path.name, **identity}


def matches(frame, answer, finish):
    if finish != "stop" or not valid_description(answer):
        return False
    normalized = answer.casefold().replace("’", "'").rstrip(".!")
    if not frame.terms:
        return normalized == "i can't tell"

    def has_any(terms):
        return any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in terms)

    return all(has_any(group) for group in frame.terms) and not has_any(frame.other_subject)


def describe(socket_path, frame):
    answer, finish, timing = vision_runtime.describe(socket_path, frame.image, frame.content_type)
    outcome = {"case": frame.label, "image_sha256": hashlib.sha256(frame.image).hexdigest(),
               "answer": answer, "finish": finish, "request_ms": timing.pop("request_ms"),
               "screening_pass": matches(frame, answer, finish)}
    if timing:
        outcome["server_timings"] = timing
    return outcome


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--projector", type=Path, required=True)
    parser.add_argument("--projector-sha256", required=True)
    parser.add_argument("--photos", type=Path, required=True, help="folder containing the two pinned NASA JPGs")
    parser.add_argument("--profile", choices=vision_runtime.PROFILES, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new JSON evidence file")
    args = parser.parse_args(argv)
    if not all(re.fullmatch(r"[0-9a-fA-F]{64}", value)
               for value in (args.model_sha256, args.projector_sha256)):
        parser.error("supply full SHA-256 values for both GGUF files")
    try:
        if os.path.lexists(args.output) or not args.output.parent.is_dir():
            raise ValueError("Choose a new report path in an existing directory.")
        binary, model, projector = (path.resolve(strict=True) for path in (args.server, args.model, args.projector))
        stamps = {path: artifact_identity(path) for path in (binary, model, projector)}
        identity = {"server": identify(binary), "model": identify(model, args.model_sha256),
                    "projector": identify(projector, args.projector_sha256)}
        frames = list(cases(args.photos))
        minimum, maximum, context = vision_runtime.PROFILES[args.profile]
        report = {"kind": "pipos-vision-audition-v1", "inputs": identity, "profile": args.profile,
                  "host": {"platform": platform.system(), "architecture": platform.machine()},
                  "limits": {"threads": 2, "context_tokens": context, "image_token_range": [minimum, maximum],
                             "answer_tokens": vision_runtime.MAX_TOKENS, "load_seconds": vision_runtime.LOAD_TIMEOUT, "turn_seconds": vision_runtime.TURN_TIMEOUT},
                  "prompt": vision_runtime.PROMPT, "cases": []}
        try:
            if any(artifact_identity(path) != stamp for path, stamp in stamps.items()):
                raise ValueError("An audition input changed before the server started.")
            with tempfile.TemporaryDirectory(prefix="pip-vlm-", dir="/tmp") as folder, \
                    vision_runtime.server(binary, model, projector, Path(folder), args.profile) as (endpoint, ready_ms):
                report["startup_ms"] = ready_ms
                for frame in frames:
                    outcome = describe(endpoint, frame)
                    report["cases"].append(outcome)
                    print(f"{frame.label}: {outcome['request_ms']} ms, screening pass={outcome['screening_pass']}")
            if any(artifact_identity(path) != stamp for path, stamp in stamps.items()):
                raise ValueError("An audition input changed while the server was running.")
        except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as error:
            report["error"] = str(error)
        except KeyboardInterrupt:
            report["error"] = "Interrupted."
        report["screening_pass"] = ("error" not in report and len(report["cases"]) == len(frames)
                                    and all(result["screening_pass"] for result in report["cases"]))
        publish(args.output, report)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if "error" in report:
        print(report["error"], file=sys.stderr)
    return int(not report["screening_pass"])


if __name__ == "__main__":
    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    sys.exit(main())

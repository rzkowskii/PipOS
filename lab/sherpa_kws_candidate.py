"""Run the pinned sherpa-onnx KWS C API over a bounded synthetic corpus.

This is a lab adapter, not a Voice backend.  It opens only named WAV/model
files, never records audio, and emits the engine block consumed by
``wake_candidate_compare.py``.  Local digests are not release signatures.
"""

import argparse
from array import array
import ctypes as C
import hashlib
import io
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import wave


VERSION = "2.0.3"
SOURCE_SHA256 = "615b1bf0140bf09051c15fde6579e48543696352ebd477217d2e69b49d9aec13"
MODEL_ID = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01-mobile"
MODEL_ARCHIVE_SHA256 = "2e6ac2577310bfa2f4b6b5fab0478b868c9d0b2cb2c51b3e13b50581b588864d"
MAX_JSON = 256 * 1024
MAX_WAV = 576 * 1024
MAX_COMPONENT = 512 * 1024**2
MAX_CASES = 512
RATE = 16_000
CHUNK = 3_200
TAIL = 4_800
SAFE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


class Transducer(C.Structure):
    _fields_ = [("encoder", C.c_char_p), ("decoder", C.c_char_p), ("joiner", C.c_char_p)]


class Paraformer(C.Structure):
    _fields_ = [("encoder", C.c_char_p), ("decoder", C.c_char_p)]


class OneFile(C.Structure):
    _fields_ = [("model", C.c_char_p)]


class Model(C.Structure):
    _fields_ = [
        ("transducer", Transducer), ("paraformer", Paraformer), ("zipformer2_ctc", OneFile),
        ("tokens", C.c_char_p), ("num_threads", C.c_int32), ("provider", C.c_char_p),
        ("debug", C.c_int32), ("model_type", C.c_char_p), ("modeling_unit", C.c_char_p),
        ("bpe_vocab", C.c_char_p), ("tokens_buf", C.c_char_p), ("tokens_buf_size", C.c_int32),
        ("nemo_ctc", OneFile), ("t_one_ctc", OneFile),
    ]


class Feature(C.Structure):
    _fields_ = [("sample_rate", C.c_int32), ("feature_dim", C.c_int32)]


class Config(C.Structure):
    _fields_ = [
        ("feat_config", Feature), ("model_config", Model), ("max_active_paths", C.c_int32),
        ("num_trailing_blanks", C.c_int32), ("keywords_score", C.c_float),
        ("keywords_threshold", C.c_float), ("keywords_file", C.c_char_p),
        ("keywords_buf", C.c_char_p), ("keywords_buf_size", C.c_int32),
    ]


class Result(C.Structure):
    _fields_ = [
        ("keyword", C.c_char_p), ("tokens", C.c_char_p), ("tokens_arr", C.POINTER(C.c_char_p)),
        ("count", C.c_int32), ("timestamps", C.POINTER(C.c_float)),
        ("start_time", C.c_float), ("json", C.c_char_p),
    ]


def digest(path):
    if not 0 < path.stat().st_size <= MAX_COMPONENT:
        raise ValueError("candidate component exceeds its size bound")
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    with path.open("rb") as source:
        raw = source.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("JSON exceeds 256 KiB")
    return json.loads(raw), raw


def regular_under(base, name):
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("model and WAV entries must be basenames")
    path = base / name
    if path.is_symlink() or not path.is_file():
        raise ValueError("input is not a regular non-symlink file")
    return path


def load_descriptor(path):
    value, raw = read_json(path)
    if (not isinstance(value, dict) or value.get("kind") != "pipos-lab-kws-pack-v1"
            or value.get("runtime_version") != VERSION):
        raise ValueError("descriptor kind or runtime version is wrong")
    if value.get("runtime_source_sha256") != SOURCE_SHA256:
        raise ValueError("descriptor runtime source digest is wrong")
    if value.get("model_id") != MODEL_ID or value.get("model_archive_sha256") != MODEL_ARCHIVE_SHA256:
        raise ValueError("descriptor is not the pinned English GigaSpeech mobile candidate")
    files = value.get("files")
    required = {"encoder", "decoder", "joiner", "tokens"}
    if (not isinstance(files, list) or len(files) != len(required)
            or any(not isinstance(item, dict) or not isinstance(item.get("role"), str) for item in files)
            or {item["role"] for item in files} != required):
        raise ValueError("descriptor needs exactly encoder, decoder, joiner, and tokens")
    resolved = {}
    records = []
    for item in files:
        if set(item) != {"role", "path", "bytes", "sha256"} or type(item["bytes"]) is not int:
            raise ValueError("invalid model file record")
        source = regular_under(path.parent, item["path"])
        if source.stat().st_size != item["bytes"] or (actual := digest(source)) != item["sha256"]:
            raise ValueError("model file size or digest differs")
        resolved[item["role"]] = source
        records.append({"role": item["role"], "sha256": actual})
    keyword = value.get("keyword_definition")
    display = value.get("keyword_display")
    if not isinstance(keyword, str) or not keyword or len(keyword.encode()) > 4096:
        raise ValueError("bounded tokenized keyword_definition is required")
    if not isinstance(display, str) or display != display.casefold() or not 0 < len(display) <= 120:
        raise ValueError("bounded casefold keyword_display is required")
    search = value.get("search")
    if not isinstance(search, dict):
        raise ValueError("search settings are required")
    paths = search.get("max_active_paths", 4)
    blanks = search.get("num_trailing_blanks", 1)
    score = search.get("keywords_score", 3.0)
    threshold = search.get("keywords_threshold", 0.1)
    if type(paths) is not int or not 1 <= paths <= 16 or type(blanks) is not int or not 0 <= blanks <= 20:
        raise ValueError("search path/blank settings are outside lab bounds")
    if (type(score) not in (int, float) or not 0 < score <= 100
            or type(threshold) not in (int, float) or not 0 < threshold <= 1):
        raise ValueError("search score/threshold exceeds lab bounds")
    return raw, resolved, sorted(records, key=lambda x: x["role"]), keyword, display, (paths, blanks, score, threshold)


def verify_cms(descriptor, raw, cms, certificate):
    if (cms is None) != (certificate is None):
        raise ValueError("--cms and --certificate must be supplied together")
    if cms is None:
        return "lab_digest_only"
    result = subprocess.run(
        ["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(cms),
         "-CAfile", str(certificate), "-content", str(descriptor)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=15, check=False,
    )
    if result.returncode or result.stdout != raw:
        raise ValueError("detached CMS did not verify against the explicit lab certificate")
    return "lab_detached_cms"


def load_corpus(path):
    value, _ = read_json(path)
    cases = None
    if isinstance(value, dict) and value.get("kind") == "pipos-wake-synthetic-corpus-v1":
        cases = value.get("cases")
    if not isinstance(cases, list) or not 2 <= len(cases) <= MAX_CASES:
        raise ValueError("synthetic corpus needs 2 to 512 cases")
    out, identity = [], []
    seen = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not SAFE.fullmatch(case["id"]) or case["id"] in seen):
            raise ValueError("invalid or duplicate case ID")
        condition = case.get("condition")
        expected = case.get("expected")
        if case.get("synthetic") is not True or condition not in {"wake", "near_miss", "unaddressed", "noise"}:
            raise ValueError("only labeled synthetic cases are accepted")
        if type(expected) is not bool or expected != (condition == "wake"):
            raise ValueError("expected must be true exactly for wake")
        wav = regular_under(path.parent, case.get("wav"))
        raw = _wav_bytes(wav)
        wav_sha = hashlib.sha256(raw).hexdigest()
        seen.add(case["id"])
        out.append((case, wav, wav_sha))
        identity.append({"id": case["id"], "condition": condition, "expected": expected, "wav_sha256": wav_sha})
    corpus_sha = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return out, corpus_sha


def _wav_bytes(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_WAV + 1)
    if len(raw) > MAX_WAV:
        raise ValueError("WAV exceeds its input bound")
    return raw


def read_wav(path):
    raw = _wav_bytes(path)
    with wave.open(io.BytesIO(raw), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != RATE:
            raise ValueError("WAV must be mono signed-16 PCM at 16 kHz")
        frames = source.getnframes()
        if not 800 <= frames <= RATE * 10:
            raise ValueError("WAV duration is outside 0.05 to 10 seconds")
        pcm = array("h", source.readframes(frames))
    if len(pcm) != frames:
        raise ValueError("WAV ended before its declared frame count")
    if sys.byteorder != "little":
        pcm.byteswap()
    return array("f", (sample / 32768.0 for sample in pcm)), frames, hashlib.sha256(raw).hexdigest()


def bind(library):
    lib = C.CDLL(str(library.resolve(strict=True)))
    pointer = C.c_void_p
    signatures = {
        "GetVersionStr": ([], C.c_char_p),
        "CreateKeywordSpotter": ([C.POINTER(Config)], pointer),
        "DestroyKeywordSpotter": ([pointer], None),
        "CreateKeywordStream": ([pointer], pointer),
        "DestroyOnlineStream": ([pointer], None),
        "OnlineStreamAcceptWaveform": ([pointer, C.c_int32, C.POINTER(C.c_float), C.c_int32], None),
        "OnlineStreamInputFinished": ([pointer], None),
        "IsKeywordStreamReady": ([pointer, pointer], C.c_int32),
        "DecodeKeywordStream": ([pointer, pointer], None),
        "GetKeywordResult": ([pointer, pointer], C.POINTER(Result)),
        "DestroyKeywordResult": ([C.POINTER(Result)], None),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, f"SherpaOnnx{name}")
        function.argtypes = arguments
        function.restype = result
    if lib.SherpaOnnxGetVersionStr() != VERSION.encode("ascii"):
        raise ValueError("loaded sherpa library is not v2.0.3")
    return lib


def run_case(lib, spotter, samples, frames, expected_keyword):
    stream = lib.SherpaOnnxCreateKeywordStream(spotter)
    if not stream:
        raise RuntimeError("Sherpa failed to create keyword stream")
    trigger = None
    fed = 0
    decodes = 0
    started = time.monotonic_ns()
    try:
        chunks = (samples[i:i + CHUNK] for i in range(0, frames, CHUNK))
        tail = array("f", [0.0]) * TAIL
        for block in itertools.chain(chunks, (tail,)):
            buf = (C.c_float * len(block)).from_buffer(block)
            lib.SherpaOnnxOnlineStreamAcceptWaveform(stream, RATE, buf, len(block))
            fed += len(block)
            if block is tail:
                lib.SherpaOnnxOnlineStreamInputFinished(stream)
            while lib.SherpaOnnxIsKeywordStreamReady(spotter, stream):
                decodes += 1
                if decodes > 2_048:
                    raise RuntimeError("Sherpa did not settle within the clip's decode bound")
                lib.SherpaOnnxDecodeKeywordStream(spotter, stream)
                result = lib.SherpaOnnxGetKeywordResult(spotter, stream)
                try:
                    keyword = result.contents.keyword if result else None
                    if trigger is None and keyword and keyword.decode("utf-8").casefold() == expected_keyword:
                        trigger = fed * 1000.0 / RATE
                finally:
                    if result:
                        lib.SherpaOnnxDestroyKeywordResult(result)
    finally:
        lib.SherpaOnnxDestroyOnlineStream(stream)
    return trigger, fed - TAIL, (time.monotonic_ns() - started) / 1e6


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--descriptor", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--cms", type=Path)
    parser.add_argument("--certificate", type=Path)
    args = parser.parse_args(argv)
    spotter = None
    try:
        raw, files, model_files, keyword, display, search = load_descriptor(args.descriptor)
        trust = verify_cms(args.descriptor, raw, args.cms, args.certificate)
        cases, corpus_sha = load_corpus(args.corpus)
        if args.library.is_symlink() or not args.library.is_file():
            raise ValueError("library must be a regular non-symlink file")
        lib = bind(args.library)
        keep = [str(files[x]).encode() for x in ("encoder", "decoder", "joiner", "tokens")]
        keyword_bytes = keyword.encode()
        model = Model(transducer=Transducer(*keep[:3]), tokens=keep[3], num_threads=1,
                      provider=b"cpu", model_type=b"zipformer")
        config = Config(Feature(RATE, 80), model, *search, None, keyword_bytes, len(keyword_bytes))
        spotter = lib.SherpaOnnxCreateKeywordSpotter(C.byref(config))
        if not spotter:
            raise RuntimeError("Sherpa failed to create keyword spotter")
        results, inputs = [], []
        for case, wav, expected_sha in cases:
            samples, frames, wav_sha = read_wav(wav)
            if wav_sha != expected_sha:
                raise ValueError("WAV changed after the corpus was checked")
            trigger, consumed, processing = run_case(lib, spotter, samples, frames, display)
            results.append({"id": case["id"], "consumed_frames": consumed, "detected": trigger is not None,
                            "trigger_audio_ms": round(trigger, 3) if trigger is not None else None,
                            "processing_ms": round(processing, 3), "score": None})
            inputs.append({"id": case["id"], "frames": frames, "sample_rate": RATE, "wav_sha256": wav_sha})
        provenance = {"version": VERSION, "source_sha256": SOURCE_SHA256, "binary_sha256": digest(args.library),
                      "corpus_sha256": corpus_sha, "api": "sherpa-onnx-c-api", "model_files": model_files,
                      "model_pack_sha256": hashlib.sha256(raw).hexdigest(), "model_trust": trust}
        print(json.dumps({"provenance": provenance, "inputs": inputs, "results": results}, sort_keys=True))
        return 0
    except (AttributeError, EOFError, OSError, ValueError, RuntimeError, wave.Error, subprocess.TimeoutExpired) as error:
        print(f"Sherpa candidate refused: {error}", file=sys.stderr)
        return 1
    finally:
        if spotter:
            lib.SherpaOnnxDestroyKeywordSpotter(spotter)


if __name__ == "__main__":
    raise SystemExit(main())

"""Measure the voice engines' speed, idle and beside a busy cortex, as JSON.

Bench mode (`being.bench=1`) runs this on the laptop. It measures:

- Silero VAD: milliseconds per 512-sample window;
- Moonshine tiny and base: real-time factor on each model's test recording;
- Piper: real-time factor on the fixed 12-word sentence, 3 runs;

each once with the machine idle and once beside a busy cortex. "Busy" is the
language model generating (a stream of short requests to the local cortex),
or, with `--busy cpu`, two spinning processes when no cortex is running. With
the cortex, its decode speed alone and beside Piper is recorded too, for the
"slows decode by no more than 15%" check. A part that cannot run is reported
with its reason; nothing is invented.

    python3 -m mind.voice_bench --busy cortex --output /run/being/voice-bench.json
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from array import array
from contextlib import contextmanager, suppress
from pathlib import Path

from . import voice_engines

CORTEX = "http://127.0.0.1:8080"
VAD_SECONDS = 10.0
SIZES = ("tiny", "base")
# A busy cortex: short requests so that decode-speed samples arrive every few seconds.
CORTEX_TOKENS = 32
# Wait this long for the first cortex response, then sample its speed alone for this long.
# On this laptop a 32-token request takes several seconds, so each window must
# hold at least two whole requests for a median to mean anything.
CORTEX_WARM_S = 8.0
CORTEX_ALONE_S = 15.0
# Piper speaks without a break for this long beside the cortex, for the decode check.
PIPER_BESIDE_S = 15.0
CORTEX_PROMPT = "Tell me, in plain words, one thing about how a lighthouse keeper spends the night."


def _rtf_summary(result):
    return {"rtf": round(result["rtf"], 4), "runs": [round(run, 4) for run in result["runs"]],
            **({"text": result["text"]} if "text" in result else {})}


# Loads ---------------------------------------------------------------------------

class CortexLoad:
    """Keep the cortex generating; record its decode speed from each response."""

    def __init__(self, url=CORTEX, tokens=CORTEX_TOKENS, opener=None):
        self.url = url.rstrip("/") + "/completion"
        self.tokens = tokens
        self.opener = opener or urllib.request.urlopen
        self.samples = []       # (started, finished, tokens per second)
        self.errors = []
        self._stop = threading.Event()
        self._thread = None

    def _request(self):
        body = json.dumps({"prompt": CORTEX_PROMPT, "n_predict": self.tokens, "cache_prompt": True,
                           "temperature": 0.7}).encode()
        request = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        started = time.monotonic()
        with self.opener(request, timeout=120) as response:
            data = json.loads(response.read())
        rate = (data.get("timings") or {}).get("predicted_per_second")
        if isinstance(rate, (int, float)) and rate > 0:
            self.samples.append((started, time.monotonic(), float(rate)))

    def _run(self):
        while not self._stop.is_set():
            try:
                self._request()
            except Exception as error:
                self.errors.append(repr(error))
                if len(self.errors) >= 3:
                    return
                self._stop.wait(1)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="being-bench-cortex", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=150)

    def rate_between(self, start, end):
        """Median decode speed of the requests that ran mostly inside [start, end]."""
        rates = []
        for began, finished, rate in list(self.samples):
            overlap = min(end, finished) - max(start, began)
            if overlap >= 0.5 * (finished - began) and overlap >= 0:
                rates.append(rate)
        return statistics.median(rates) if rates else None

    def describe(self):
        return {"kind": "cortex", "url": self.url, "requests": len(self.samples), "errors": self.errors[:3]}


class CpuLoad:
    """Two spinning processes, one per core, when there is no cortex to keep busy."""

    def __init__(self, workers=2):
        self.workers = workers
        self.processes = []

    def start(self):
        self.processes = [subprocess.Popen([sys.executable, "-c", "while True: pass"]) for _ in range(self.workers)]

    def stop(self):
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)

    def rate_between(self, start, end):
        return None

    def describe(self):
        return {"kind": "cpu", "workers": self.workers}


@contextmanager
def loaded(load):
    load.start()
    try:
        yield load
    finally:
        load.stop()


# Parts --------------------------------------------------------------------------

def bench_vad(vad, samples, window=512, clock=time.perf_counter):
    windows = len(samples) // window
    if windows == 0:
        raise ValueError("no audio for the voice activity check")
    start = clock()
    for index in range(windows):
        vad.accept(samples[index * window:(index + 1) * window])
        vad.pop_segments()
    elapsed = clock() - start
    vad.reset()
    return {"windows": windows, "ms_per_window": round(1000 * elapsed / windows, 4),
            "rtf": round(elapsed / (windows * window / 16000), 4)}


def speak_for(piper, seconds, clock=time.monotonic):
    """Keep Piper speaking the fixed sentence for `seconds`; return the window and runs."""
    runs = 0
    with tempfile.TemporaryDirectory(prefix="being-bench-") as folder:
        target = Path(folder) / "beside.wav"
        start = clock()
        while clock() - start < seconds or runs == 0:
            piper.synthesize_blocking(voice_engines.SENTENCE, target, 1.0)
            runs += 1
        return start, clock(), runs


def test_audio(wav=None, seconds=VAD_SECONDS):
    """The test recording repeated to `seconds`, or speech-like bursts if none is given."""
    if wav is not None:
        base = voice_engines.read_wav_16k(wav)
    else:
        base = array("f", ((0.3 if (i // 40) % 2 else -0.3) if (i // 8000) % 2 else 0.0 for i in range(32000)))
    out = array("f")
    while len(out) < seconds * 16000:
        out.extend(base)
    return out[:int(seconds * 16000)]


def run(*, asr_root=None, voice_dir=None, busy="cortex", cortex=CORTEX, runs=voice_engines.RUNS,
        sizes=SIZES, vad_factory=None, asr_factory=None, tts_factory=None, load_factory=None, library=None):
    """Measure everything that is installed and return the report as a dict."""
    from . import asr_sherpa, sherpa, tts_sherpa

    asr_root = Path(asr_root) if asr_root else asr_sherpa.ROOT
    report = {"version": 1, "build": voice_engines.build_id(), "limits": {
        "piper_idle_rtf": voice_engines.TTS_RTF_LIMIT, "moonshine_idle_rtf": voice_engines.ASR_RTF_LIMIT,
        "piper_busy_rtf": 0.5, "decode_slowdown_pct": 15}, "errors": {}}
    try:
        report["library"] = sherpa.version(library)
    except sherpa.Unavailable as error:
        report["errors"]["library"] = error.reason
        if vad_factory is None and asr_factory is None and tts_factory is None:
            return report

    vad_factory = vad_factory or (lambda: asr_sherpa.load_vad(asr_root, library))
    asr_factory = asr_factory or (lambda size: asr_sherpa.load_moonshine(asr_root, size, library))
    tts_factory = tts_factory or (lambda: tts_sherpa.PiperEngine(voice_dir or tts_sherpa.DEFAULT_VOICE,
                                                                 library=library))
    if load_factory is None:
        def load_factory():
            return CortexLoad(cortex) if busy == "cortex" else CpuLoad() if busy == "cpu" else None

    def attempt(name, measure):
        try:
            return measure()
        except Exception as error:
            report["errors"][name] = getattr(error, "reason", None) or str(error) or repr(error)
            return None

    wavs = {size: asr_sherpa.test_wav(asr_root, size) for size in sizes}
    recognizers = {}
    for size in sizes:
        if wavs[size] is None:
            report["errors"][f"moonshine_{size}"] = "no test recording in the model folder"
            continue
        recognizer = attempt(f"moonshine_{size}", lambda size=size: asr_factory(size))
        if recognizer is not None:
            recognizers[size] = recognizer
    vad = attempt("vad", vad_factory)
    piper = attempt("piper", tts_factory)
    if piper is not None and not piper.available():
        report["errors"]["piper"] = "the natural voice is not installed"
        piper = None
    if piper is not None and attempt("piper", lambda: piper.resident() or True) is None:
        piper = None        # the voice would not load; the reason is in errors
    audio = attempt("audio", lambda: test_audio(next((w for w in wavs.values() if w is not None), None))) \
        or test_audio(None)

    def measure_all(phase):
        if vad is not None:
            result = attempt(f"vad_{phase}", lambda: bench_vad(vad, audio))
            if result:
                report.setdefault("vad", {})[phase] = result
        for size, recognizer in recognizers.items():
            result = attempt(f"moonshine_{size}_{phase}",
                             lambda: voice_engines.measure_asr(recognizer, wavs[size], runs=runs))
            if result:
                report.setdefault(f"moonshine_{size}", {})[phase] = _rtf_summary(result)
        if piper is not None:
            started = time.monotonic()
            result = attempt(f"piper_{phase}", lambda: voice_engines.measure_tts(piper, runs=runs))
            if result:
                report.setdefault("piper", {})[phase] = {**_rtf_summary(result),
                                                        "window": (started, time.monotonic())}

    measure_all("idle")
    load = load_factory() if busy != "none" else None
    if load is not None:
        try:
            with loaded(load):
                warm_until = time.monotonic() + (CORTEX_WARM_S if isinstance(load, CortexLoad) else 1)
                while time.monotonic() < warm_until and not getattr(load, "samples", True):
                    time.sleep(0.2)
                alone_start = time.monotonic()
                if isinstance(load, CortexLoad):
                    # Decode speed with no voice work beside it.
                    time.sleep(CORTEX_ALONE_S)
                alone_end = time.monotonic()
                measure_all("busy")
                beside = None
                if isinstance(load, CortexLoad) and piper is not None:
                    beside = attempt("piper_beside", lambda: speak_for(piper, PIPER_BESIDE_S))
            report["busy"] = load.describe()
            window = beside[:2] if beside else report.get("piper", {}).get("busy", {}).get("window")
            alone = load.rate_between(alone_start, alone_end)
            beside_rate = load.rate_between(*window) if window else None
            if beside:
                report["busy"]["piper_beside_sentences"] = beside[2]
            if alone and beside_rate:
                report["busy"].update(decode_tps_alone=round(alone, 3),
                                      decode_tps_beside_piper=round(beside_rate, 3),
                                      decode_slowdown_pct=round(100 * (alone - beside_rate) / alone, 2))
        except Exception as error:
            report["errors"]["busy"] = repr(error)
    for phase in report.get("piper", {}).values():
        phase.pop("window", None)
    for part in [vad, piper, *recognizers.values()]:
        with suppress(Exception):
            part.close()
    report["verdict"] = verdict(report)
    return report


def verdict(report):
    """Pass or fail against the limits, only for what was measured."""
    limits = report["limits"]
    out = {}
    piper = report.get("piper", {})
    if "idle" in piper:
        out["piper_auto"] = piper["idle"]["rtf"] <= limits["piper_idle_rtf"]
    if "busy" in piper:
        out["piper_busy"] = piper["busy"]["rtf"] <= limits["piper_busy_rtf"]
    for size in SIZES:
        part = report.get(f"moonshine_{size}", {})
        if "idle" in part:
            out[f"moonshine_{size}_auto"] = part["idle"]["rtf"] <= limits["moonshine_idle_rtf"]
    slowdown = report.get("busy", {}).get("decode_slowdown_pct")
    if slowdown is not None:
        out["decode_slowdown"] = slowdown <= limits["decode_slowdown_pct"]
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="voice_bench", description=__doc__.split("\n\n")[0])
    parser.add_argument("--asr-root", type=Path, help="folder with silero_vad.onnx and moonshine-*/")
    parser.add_argument("--voice", type=Path, help="Piper voice folder")
    parser.add_argument("--busy", choices=("cortex", "cpu", "none"), default="cortex")
    parser.add_argument("--cortex", default=CORTEX)
    parser.add_argument("--runs", type=int, default=voice_engines.RUNS)
    parser.add_argument("--sizes", default=",".join(SIZES), help="Moonshine sizes, comma separated")
    parser.add_argument("--output", type=Path, help="write the JSON here as well as to stdout")
    args = parser.parse_args(argv)
    sizes = tuple(size for size in args.sizes.split(",") if size)
    if any(size not in SIZES for size in sizes) or args.runs < 1:
        parser.error("unknown size or bad run count")
    report = run(asr_root=args.asr_root, voice_dir=args.voice, busy=args.busy, cortex=args.cortex,
                 runs=args.runs, sizes=sizes)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=args.output.parent, prefix=".voice-bench-",
                                         delete=False) as target:
            target.write(text + "\n")
        os.replace(target.name, args.output)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

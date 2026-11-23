"""PipOS bench mode: measure the real laptop from a Try stick (`being.bench=1`).

This file runs ON THE LAPTOP (the os-image lane installs it as
/usr/lib/being/bench.py and starts it from being-bench.service when the kernel
command line holds being.bench=1). It is Python standard library only.

What it measures, writing JSON to this boot's PIPLOGS folder as it goes
(bench-<time>.json, rewritten after every step so a power cut keeps the
finished parts):

* system facts: CPU, threads, memory, kernel, governor, turbo and caps, RAPL;
* per model and setting, a private llama-server started with the production
  flags (being-cortex.service plus LLAMA_ARG_BATCH=512) on a private port:
  load time, cold prefill at 16-512 tokens, decode 64 tokens, a 6-turn chat
  with prompt and cached tokens per turn, slot save and restore (cache_n after
  the restore), and cancel-to-release;
* speech real-time factors (Flite synthesis, PocketSphinx decoding) while the
  model decodes in the background;
* optionally a soak at turbo, at 2.5 GHz and at 1.8 GHz with RAPL tokens per
  joule, on AC only, each step stopped early if the package reaches 95 C.
  Every power setting it changes is restored on exit, on SIGTERM and on any
  error.

Models: GGUF files listed in a manifest.json on the bench partition (label
PIPBENCH, mounted read-only by the service at /run/being-bench/models), else
the shipped /models/reflex.gguf. Manifest format:

    {"models": [{"file": "minicpm5-1b-q4_0.gguf", "name": "MiniCPM5-1B Q4_0",
                 "settings": [{"threads": 2, "threads_batch": 2},
                              {"threads": 4, "threads_batch": 4}]}]}

It prints no conversation text and records no audio: the prompts are fixed
filler and the speech test decodes Pip's own synthesised voice.
"""

import argparse
import atexit
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

KIND = "pipos-bench-v1"
SERVER = "/usr/bin/llama-server"
PORT = 8091
PREFILL_SIZES = (16, 64, 128, 256, 512)
DECODE_TOKENS = 64
SOAK_CAPS = (("turbo", None), ("2.5GHz", 2500000), ("1.8GHz", 1800000))
FILLER = ("The small laptop sat on the desk and watched the morning light move across the wall. "
          "It counted the seconds, listened to the fan, and thought about the shape of the day. ")
TURNS = ("Hello Pip. How are you this morning?",
         "What did you notice while I was away?",
         "Tell me one thing you like about the rain.",
         "Can you count to five for me?",
         "What is your favourite colour, and why?",
         "Thank you. Goodnight, Pip.")
SYSTEM = ("You are Pip, a small being who lives in this laptop. You answer in one or two short "
          "sentences, warmly and honestly.")
PIPLOGS_STATE = Path("/run/being-usb-logs/folder")
CPU = Path("/sys/devices/system/cpu")
PSTATE = CPU / "intel_pstate"
RAPL = Path("/sys/class/powercap/intel-rapl:0")


# --- small helpers -----------------------------------------------------------

def read(path, default=None):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def write(path, value):
    Path(path).write_text(str(value), encoding="utf-8")


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Report:
    """The JSON file, rewritten atomically after every step."""

    def __init__(self, folder, data):
        self.path = Path(folder) / f"bench-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}.json"
        self.data = data
        self.save()

    def save(self):
        tmp = self.path.with_name("." + self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


# --- system facts --------------------------------------------------------------

def cpu_facts(root=Path("/")):
    text = read(root / "proc/cpuinfo", "") or ""
    models = re.findall(r"^model name\s*:\s*(.+)$", text, re.M)
    flags = re.search(r"^flags\s*:\s*(.+)$", text, re.M)
    cores = {m for m in re.findall(r"^core id\s*:\s*(\d+)$", text, re.M)}
    return {"model": models[0] if models else None, "threads": len(models), "physical_cores": len(cores) or None,
            "avx": bool(flags and " avx " in f" {flags[1]} "), "avx2": bool(flags and " avx2 " in f" {flags[1]} ")}


def power_facts(root=Path("/")):
    cpu = root / str(CPU).lstrip("/")
    pstate = root / str(PSTATE).lstrip("/")
    rapl = root / str(RAPL).lstrip("/")
    facts = {
        "scaling_driver": read(cpu / "cpu0/cpufreq/scaling_driver"),
        "governor": read(cpu / "cpu0/cpufreq/scaling_governor"),
        "max_freq_khz": read(cpu / "cpu0/cpufreq/cpuinfo_max_freq"),
        "scaling_max_freq_khz": read(cpu / "cpu0/cpufreq/scaling_max_freq"),
        "intel_pstate_status": read(pstate / "status"),
        "no_turbo": read(pstate / "no_turbo"),
        "max_perf_pct": read(pstate / "max_perf_pct"),
        "rapl": None,
    }
    if (rapl / "energy_uj").exists():
        facts["rapl"] = {"name": read(rapl / "name"),
                         "pl1_uw": read(rapl / "constraint_0_power_limit_uw"),
                         "pl2_uw": read(rapl / "constraint_1_power_limit_uw"),
                         "enabled": read(rapl / "enabled")}
    return facts


def system_facts(root=Path("/")):
    mem = re.search(r"^MemTotal:\s*(\d+)", read(root / "proc/meminfo", "") or "", re.M)
    return {"cpu": cpu_facts(root), "power": power_facts(root),
            "memory_mib": int(mem[1]) // 1024 if mem else None,
            "kernel": platform.release(), "cmdline": read(root / "proc/cmdline"),
            "build_id": read(root / "etc/being/build-id"), "source_id": read(root / "etc/being/source-id"),
            "dmi": read(root / "sys/class/dmi/id/product_name")}


def package_temp(root=Path("/")):
    """coretemp 'Package id 0' in degrees C, or the hottest core."""
    best = None
    for hwmon in sorted((root / "sys/class/hwmon").glob("hwmon*")):
        if read(hwmon / "name") != "coretemp":
            continue
        for label in hwmon.glob("temp*_label"):
            value = read(label.with_name(label.name.replace("_label", "_input")))
            if value and value.isdigit():
                celsius = int(value) / 1000
                if read(label) == "Package id 0":
                    return celsius
                best = max(best or celsius, celsius)
    return best


def on_mains(root=Path("/")):
    """True on AC, False on battery, None when no charger is reported."""
    found = None
    for supply in sorted((root / "sys/class/power_supply").glob("*")):
        if read(supply / "type") == "Mains":
            online = read(supply / "online")
            if online in ("0", "1"):
                found = bool(found) or online == "1"
    return found


HOT_C = 95      # stop a soak step early; the CPU's own limit is 105 degrees C


class Energy:
    """RAPL package energy, with counter wrap."""

    def __init__(self, root=RAPL):
        self.root = Path(root)
        self.range = int(read(self.root / "max_energy_range_uj", "0") or 0)

    def available(self):
        return (self.root / "energy_uj").exists()

    def read(self):
        value = read(self.root / "energy_uj")
        return int(value) if value and value.isdigit() else None

    def joules(self, start, end):
        if start is None or end is None:
            return None
        delta = end - start
        if delta < 0 and self.range:
            delta += self.range
        return delta / 1e6


# --- power caps, always restored ----------------------------------------------

class PowerCaps:
    """Changes no_turbo and per-CPU scaling_max_freq; restore() puts them back."""

    def __init__(self, cpu=CPU, pstate=PSTATE):
        self.cpu, self.pstate = Path(cpu), Path(pstate)
        self.saved = {}

    def _remember(self, path):
        if path not in self.saved:
            value = read(path)
            if value is not None:
                self.saved[path] = value

    def set(self, max_khz):
        """max_khz None means turbo on and no frequency cap."""
        no_turbo = self.pstate / "no_turbo"
        if no_turbo.exists():
            self._remember(no_turbo)
            write(no_turbo, 0 if max_khz is None else 1)
        for policy in sorted(self.cpu.glob("cpu[0-9]*/cpufreq/scaling_max_freq")):
            self._remember(policy)
            top = read(policy.with_name("cpuinfo_max_freq"))
            write(policy, top if max_khz is None or not top else min(int(top), max_khz))

    def restore(self):
        errors = []
        for path, value in list(self.saved.items()):
            try:
                write(path, value)
                del self.saved[path]
            except OSError as error:
                errors.append(f"{path}: {error}")
        return errors


# --- the model server ---------------------------------------------------------

def server_command(model, setting, port, slot_dir, server=SERVER):
    return [server, "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", "4096", "--parallel", "1",
            "--threads", str(setting.get("threads", 2)),
            "--threads-batch", str(setting.get("threads_batch", setting.get("threads", 2))),
            "--cache-ram", "0", "--no-cache-idle-slots", "--reasoning-budget", "0",
            "--slot-save-path", str(slot_dir), "--no-webui", *setting.get("extra_args", [])]


def server_env(setting):
    env = dict(os.environ)
    env.update(LLAMA_ARG_CTX_CHECKPOINTS="6", LLAMA_ARG_BATCH=str(setting.get("batch", 512)),
               LLAMA_ARG_UBATCH=str(setting.get("ubatch", 512)))
    if setting.get("mlock"):
        env["LLAMA_ARG_MLOCK"] = "1"
    return env


class Client:
    def __init__(self, port, timeout=900):
        self.port, self.timeout = port, timeout

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, body=payload, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            data = response.read()
            if response.status >= 400:
                raise RuntimeError(f"{method} {path}: HTTP {response.status} {data[:200]!r}")
            return json.loads(data) if data else {}
        finally:
            connection.close()

    def healthy(self):
        try:
            self.request("GET", "/health")
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def tokenize(self, text):
        return self.request("POST", "/tokenize", {"content": text})["tokens"]

    def complete(self, prompt, n_predict, *, cache=True, seed=1):
        return self.request("POST", "/completion", {
            "prompt": prompt, "n_predict": n_predict, "cache_prompt": cache, "temperature": 0,
            "seed": seed, "id_slot": 0, "ignore_eos": n_predict > 1, "stream": False})

    def stream(self, prompt, n_predict, *, stop_after=None):
        """Stream a completion. Returns (first_token_s, timings, closed_early_at)."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        started = time.monotonic()
        first, timings, chunks = None, {}, 0
        try:
            connection.request("POST", "/completion", body=json.dumps({
                "prompt": prompt, "n_predict": n_predict, "cache_prompt": True, "temperature": 0,
                "seed": 1, "id_slot": 0, "stream": True}).encode(), headers={"Content-Type": "application/json"})
            sock = connection.sock          # http.client drops this reference for streamed replies
            response = connection.getresponse()
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip() or "{}")
                if event.get("content") and first is None:
                    first = time.monotonic() - started
                if event.get("content"):
                    chunks += 1
                if event.get("timings"):
                    timings = event["timings"]
                if event.get("stop"):
                    break
                if stop_after and chunks >= stop_after:
                    closed = time.monotonic()
                    sock.shutdown(socket.SHUT_RDWR)
                    return first, timings, closed
        finally:
            connection.close()
        return first, timings, None

    def slot(self, action, filename):
        return self.request("POST", f"/slots/0?action={action}", {"filename": filename})


def start_server(model, setting, port, slot_dir, *, server=SERVER, timeout=600):
    command = server_command(model, setting, port, slot_dir, server)
    started = time.monotonic()
    process = subprocess.Popen(command, env=server_env(setting), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    client = Client(port)
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            raise RuntimeError(f"llama-server exited with {process.returncode} while loading")
        if client.healthy():
            return process, client, time.monotonic() - started
        time.sleep(0.2)
    stop_server(process)
    raise RuntimeError("llama-server did not become healthy")


def stop_server(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def timing(result):
    t = result.get("timings", {})
    return {"prompt_n": t.get("prompt_n"), "cache_n": t.get("cache_n"), "prompt_ms": t.get("prompt_ms"),
            "prompt_tps": t.get("prompt_per_second"), "predicted_n": t.get("predicted_n"),
            "predicted_tps": t.get("predicted_per_second")}


def filler_tokens(client, count):
    tokens = client.tokenize(FILLER)
    while len(tokens) < count:
        tokens = tokens + tokens
    return tokens[:count]


def chat_prompt(history):
    parts = [f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"]
    for role, text in history:
        parts.append(f"<|im_start|>{role}\n{text}<|im_end|>\n")
    return "".join(parts) + "<|im_start|>assistant\n"


def bench_model(client, slot_dir, *, sizes=PREFILL_SIZES, decode=DECODE_TOKENS, turns=TURNS, save=None):
    save = save or (lambda result: None)
    result = {"prefill": [], "turns": []}
    # Cold prefill: the cache is off, so every token is read.
    for count in sizes:
        tokens = filler_tokens(client, count)
        result["prefill"].append({"tokens": count, **timing(client.complete(tokens, 1, cache=False))})
        save(result)
    result["decode"] = timing(client.complete(filler_tokens(client, 16), decode, cache=False))
    save(result)
    # A 6-turn chat on the cache, the way the mind uses the slot.
    history = []
    for number, text in enumerate(turns, 1):
        history.append(("user", text))
        first, timings, _ = client.stream(chat_prompt(history), 48)
        reply_tokens = timings.get("predicted_n")
        history.append(("assistant", "(reply)"))      # never the reply text: fixed, private-free
        result["turns"].append({"turn": number, "first_token_s": first, "prompt_n": timings.get("prompt_n"),
                                "cache_n": timings.get("cache_n"), "predicted_n": reply_tokens,
                                "prompt_tps": timings.get("prompt_per_second"),
                                "predicted_tps": timings.get("predicted_per_second")})
        save(result)
    # Slot save and restore, then how much of the prompt the restore kept.
    name = "bench.slot"
    try:
        started = time.monotonic()
        saved = client.slot("save", name)
        save_s = time.monotonic() - started
        client.complete(filler_tokens(client, 64), 1, cache=False)        # evict the slot
        started = time.monotonic()
        restored = client.slot("restore", name)
        restore_s = time.monotonic() - started
        after = timing(client.complete(chat_prompt(history), 1))
        result["slot"] = {"save_s": round(save_s, 3), "restore_s": round(restore_s, 3),
                          "saved_tokens": saved.get("n_saved"), "restored_tokens": restored.get("n_restored"),
                          "cache_n_after_restore": after["cache_n"], "prompt_n_after_restore": after["prompt_n"]}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        result["slot"] = {"error": str(error)}
    finally:
        try:
            (Path(slot_dir) / name).unlink()
        except OSError:
            pass
    save(result)
    # Cancel-to-release: drop a long reply after a few tokens, then time a tiny request.
    try:
        started = time.monotonic()
        client.complete(filler_tokens(client, 8), 1, cache=False)
        baseline = time.monotonic() - started
        _, _, closed = client.stream(chat_prompt(history + [("user", "Tell me a long story.")]), 512, stop_after=4)
        client.complete(filler_tokens(client, 8), 1, cache=False)
        result["cancel_to_release_s"] = round(time.monotonic() - closed - baseline, 3) if closed else None
        result["tiny_request_s"] = round(baseline, 3)
    except (OSError, RuntimeError, ValueError) as error:
        result["cancel_error"] = str(error)
    save(result)
    return result


# --- speech -------------------------------------------------------------------

def wav_seconds(path):
    with wave.open(str(path)) as audio:
        return audio.getnframes() / audio.getframerate()


def to_16k_mono(path):
    """Little-endian 16-bit PCM at 16 kHz, by linear interpolation (stdlib only)."""
    import array
    with wave.open(str(path)) as audio:
        rate, channels, width = audio.getframerate(), audio.getnchannels(), audio.getsampwidth()
        frames = audio.readframes(audio.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit audio")
    samples = array.array("h", frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array.array("h", samples[::channels])
    if rate == 16000:
        return samples
    out = array.array("h")
    step = rate / 16000
    position = 0.0
    while position < len(samples) - 1:
        low = int(position)
        frac = position - low
        out.append(int(samples[low] * (1 - frac) + samples[low + 1] * frac))
        position += step
    return out


def speech_rtf(text="Hello. I am Pip, and I live in this laptop. It is nice to meet you.",
               flite="flite", mind_path="/usr/lib/being"):
    result = {}
    with tempfile.TemporaryDirectory(prefix="being-bench-") as scratch:
        audio = Path(scratch) / "pip.wav"
        if not shutil.which(flite):
            return {"error": "flite not found"}
        started = time.monotonic()
        subprocess.run([flite, "-voice", "kal16", "-t", text, "-o", str(audio)], check=True,
                       capture_output=True, timeout=120)
        took = time.monotonic() - started
        seconds = wav_seconds(audio)
        result["tts"] = {"seconds_audio": round(seconds, 3), "seconds_cpu": round(took, 3),
                         "rtf": round(took / seconds, 3) if seconds else None}
        try:
            if mind_path not in sys.path:
                sys.path.insert(0, mind_path)
            from mind.recognition import Recognizer
            import ctypes
            samples = to_16k_mono(audio)
            recognizer = Recognizer("pip")
            lib, decoder = recognizer.lib, recognizer.decoder
            started = time.monotonic()
            lib.ps_start_utt(decoder)
            buffer = (ctypes.c_int16 * len(samples)).from_buffer(samples)
            lib.ps_process_raw(decoder, buffer, len(samples), 0, 1)
            lib.ps_end_utt(decoder)
            hyp = lib.ps_get_hyp(decoder, None)
            took = time.monotonic() - started
            audio_s = len(samples) / 16000
            # The hypothesis is Pip's own fixed sentence, so word count is safe to keep.
            result["asr"] = {"seconds_audio": round(audio_s, 3), "seconds_cpu": round(took, 3),
                             "rtf": round(took / audio_s, 3) if audio_s else None,
                             "words_heard": len((hyp or b"").split()), "words_said": len(text.split())}
        except Exception as error:      # ASR is optional: never lose the TTS number
            result["asr"] = {"error": f"{type(error).__name__}: {error}"}
    return result


class Busy:
    """Keeps the model decoding in the background ("with the model busy")."""

    def __init__(self, client):
        self.client, self.stop, self.tokens = client, threading.Event(), 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        prompt = filler_tokens(self.client, 32)
        while not self.stop.is_set():
            try:
                self.tokens += timing(self.client.complete(prompt, 32, cache=False))["predicted_n"] or 0
            except (OSError, RuntimeError, ValueError):
                time.sleep(1)

    def __enter__(self):
        self.thread.start()
        time.sleep(2)
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=120)


# --- soak ---------------------------------------------------------------------

def soak(client, caps, minutes, energy, *, caps_list=SOAK_CAPS, temp=package_temp, save=None):
    results = []
    for name, khz in caps_list:
        caps.set(khz)
        prompt = filler_tokens(client, 32)
        start_energy, started, tokens, hottest = energy.read(), time.monotonic(), 0, None
        stopped = None
        while time.monotonic() - started < minutes * 60:
            tokens += timing(client.complete(prompt, 128, cache=False))["predicted_n"] or 0
            reading = temp()
            hottest = reading if hottest is None or (reading or 0) > hottest else hottest
            if reading is not None and reading >= HOT_C:
                stopped = f"package reached {reading:.0f} C"
                break
        joules = energy.joules(start_energy, energy.read())
        elapsed = time.monotonic() - started
        results.append({"cap": name, "seconds": round(elapsed, 1), "tokens": tokens, "stopped_early": stopped,
                        "tokens_per_s": round(tokens / elapsed, 2) if elapsed else None,
                        "joules": round(joules, 1) if joules is not None else None,
                        "watts": round(joules / elapsed, 2) if joules is not None and elapsed else None,
                        "tokens_per_joule": round(tokens / joules, 3) if joules else None,
                        "max_package_c": hottest})
        if save:
            save(results)
    errors = caps.restore()
    if errors:
        results.append({"restore_errors": errors})
    return results


# --- the run ------------------------------------------------------------------

def load_models(models_dir, fallback):
    manifest = Path(models_dir) / "manifest.json" if models_dir else None
    if manifest and manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        found = []
        for entry in data.get("models", []):
            path = Path(models_dir) / entry["file"]
            if path.parent.resolve() != Path(models_dir).resolve():
                raise ValueError(f"model file must sit beside the manifest: {entry['file']}")
            found.append({"path": path, "name": entry.get("name", entry["file"]),
                          "sha256": entry.get("sha256"),
                          "settings": entry.get("settings") or [{"threads": 2, "threads_batch": 2}]})
        return found
    if fallback and Path(fallback).is_file():
        return [{"path": Path(fallback), "name": "shipped reflex.gguf", "sha256": None,
                 "settings": [{"threads": 2, "threads_batch": 2}]}]
    return []


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def output_folder(explicit):
    if explicit:
        return Path(explicit)
    folder = read(PIPLOGS_STATE)
    if folder and Path(folder).is_dir():
        return Path(folder)
    fallback = Path("/run/being-bench")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def cmdline_options(cmdline):
    """being.bench.soak=MINUTES and being.bench.quick=1 on the kernel command line."""
    options = {}
    for word in (cmdline or "").split():
        key, _, value = word.partition("=")
        if key == "being.bench.soak" and value.isdigit():
            options["soak_minutes"] = int(value)
        elif key == "being.bench.quick" and value == "1":
            options["quick"] = True
    return options


def run(args):
    caps = PowerCaps()

    def restore(*_):
        caps.restore()

    atexit.register(restore)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    data = {"kind": KIND, "started": now(), "system": system_facts(), "models": [], "finished": None}
    report = Report(output_folder(args.output), data)
    options = cmdline_options(data["system"]["cmdline"])
    soak_minutes = args.soak_minutes if args.soak_minutes is not None else options.get("soak_minutes", 0)
    sizes = (16, 128) if (args.quick or options.get("quick")) else PREFILL_SIZES
    slot_dir = Path(tempfile.mkdtemp(prefix="being-bench-slots-", dir=args.scratch))
    try:
        models = load_models(args.models_dir, args.fallback_model)
        if not models:
            data["error"] = "no models found"
        for model in models:
            for setting in model["settings"]:
                entry = {"name": model["name"], "file": model["path"].name, "setting": setting,
                         "bytes": model["path"].stat().st_size}
                if model["sha256"]:
                    entry["sha256_expected"] = model["sha256"]
                    entry["sha256_ok"] = sha256(model["path"]) == model["sha256"]
                data["models"].append(entry)
                report.save()
                process = None
                try:
                    process, client, load_s = start_server(model["path"], setting, args.port, slot_dir,
                                                          server=args.server)
                    entry["load_s"] = round(load_s, 2)
                    entry.update(bench_model(client, slot_dir, sizes=sizes,
                                             save=lambda part: (entry.update(part), report.save())))
                    if args.speech and setting is model["settings"][0]:
                        with Busy(client) as busy:
                            entry["speech_with_model_busy"] = speech_rtf(mind_path=args.mind_path)
                            entry["speech_with_model_busy"]["background_tokens"] = busy.tokens
                    if soak_minutes and setting is model["settings"][0] and model is models[0]:
                        if on_mains() is False:
                            # A long full-load soak on the old battery is not worth its heat or wear.
                            entry["soak"] = [{"skipped": "on battery: plug in the charger for the soak"}]
                        else:
                            energy = Energy()
                            entry["soak"] = soak(client, caps, soak_minutes, energy,
                                                 save=lambda part: (entry.__setitem__("soak", part), report.save()))
                except Exception as error:      # one model failing never loses the others
                    entry["error"] = f"{type(error).__name__}: {error}"
                finally:
                    stop_server(process)
                    report.save()
        if args.speech and not models:
            data["speech_idle"] = speech_rtf(mind_path=args.mind_path)
    finally:
        errors = caps.restore()
        if errors:
            data["restore_errors"] = errors
        data["power_after"] = power_facts()
        data["finished"] = now()
        report.save()
        shutil.rmtree(slot_dir, ignore_errors=True)
    print(f"bench: wrote {report.path}", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", help="folder for bench-*.json (default: this boot's PIPLOGS folder)")
    parser.add_argument("--models-dir", default="/run/being-bench/models", help="folder with manifest.json")
    parser.add_argument("--fallback-model", default="/models/reflex.gguf")
    parser.add_argument("--server", default=SERVER)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--scratch", default=None, help="where slot files go (default: the system temp dir)")
    parser.add_argument("--mind-path", default="/usr/lib/being")
    parser.add_argument("--soak-minutes", type=int, default=None, help="per cap; default from being.bench.soak=")
    parser.add_argument("--quick", action="store_true", help="two prefill sizes only")
    parser.add_argument("--no-speech", dest="speech", action="store_false")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())

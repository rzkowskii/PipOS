"""Run the voice's sherpa-onnx engines on an Ivy Bridge guest and check for SIGILL.

The laptop's CPU has AVX but no AVX2, FMA or BMI. This boots a disposable
overlay of the lab image under QEMU `-cpu IvyBridge`, checks the guest's CPU
flags, and then, through the same ctypes bindings the voice uses
(mind/sherpa.py), runs each engine in its own Python process:

1. Silero VAD over the Moonshine test recording (at least one segment);
2. a Moonshine v2 tiny decode of `test_wavs/0.wav` (non-empty text);
3. one sentence in the Piper voice (a WAV of positive length), using the
   guest's packaged piper-phonemize library.

A stage killed by SIGILL (exit status 132) is named in the report. The
assets are the downloads in out/voice, checked against
lab/sherpa_avx_assets.json and out/voice/DOWNLOADS.json; nothing is fetched.
QEMU's speed says nothing about the laptop's: timings are recorded only as
"emulated".

Besides the full report (--output), the run writes a verdict the image may
ship as /usr/share/being/voice/sherpa-avx-smoke.json (--verdict): the stages'
pass or fail, SIGILL, and the sha256 of the two runtime libraries it tested,
with no host paths. The voice's automatic choice loads Moonshine only when
those pins match the installed libraries byte for byte
(mind/voice_engines.sherpa_smoke_passed). Use --runtime DIR with the two
libraries from the build's target tree for a verdict intended for an image.
Buildroot can change their bytes during packaging, including builds that
exclude the libraries from stripping.

    python3 -m lab.sherpa_avx_smoke --prepare-only /tmp/media   # stage and check, no VM
    python3 -m lab.sherpa_avx_smoke                             # boot and run
"""

import argparse
import hashlib
import json
import posixpath
import shutil
import signal
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from lab.vm import ROOT, VM

ASSETS = ROOT / "out/voice"
MANIFEST = Path(__file__).with_name("sherpa_avx_assets.json")
BINDINGS = ROOT / "mind/sherpa.py"
PHONEMIZER = ROOT / "mind/phonemize.py"
GUEST_MEDIA = "/mnt/updates"
REQUIRED = {"avx", "f16c", "sse4_2"}
EXCLUDED = {"avx2", "fma", "bmi1", "bmi2", "avx512f"}
STAGES = ("vad", "moonshine", "piper")
SIGILL_STATUS = 128 + signal.SIGILL
SENTENCE = "Hello. I am Pip, and this is my natural voice."
MARK = "SMOKE "
RUNTIME_FILES = ("libsherpa-onnx-c-api.so", "libonnxruntime.so")
VERDICT_VERSION = 1

# Runs inside the guest, from the read-only media: python3 guest.py <media> <stage>.
GUEST_SCRIPT = r'''
import json, sys, tempfile, time
from pathlib import Path
from mind import sherpa

media, stage = sys.argv[1], sys.argv[2]
library = media + "/runtime/libsherpa-onnx-c-api.so"
models = media + "/models"


def recording():
    # The Moonshine pack's test_wavs/0.wav is 24 kHz mono: the recognizer takes
    # it at its own rate, and Silero gets it resampled to 16 kHz.
    return sherpa.read_wav(media + "/fixtures/0.wav")


def runtime_paths():
    with open("/proc/self/maps") as maps:
        return sorted({line.split()[-1] for line in maps if "libonnxruntime" in line or "libsherpa" in line})


report = {"stage": stage, "versions": sherpa.version(library)}
start = time.perf_counter()
if stage == "vad":
    samples, rate = recording()
    samples = sherpa.resample(samples, rate, 16000)
    vad = sherpa.Vad(models + "/silero_vad.onnx", library=library)
    segments = []
    for index in range(0, len(samples) - 511, 512):
        vad.accept(samples[index:index + 512])
        segments += vad.pop_segments()
    vad.flush()
    segments += vad.pop_segments()
    report.update(segments=len(segments), speech_seconds=round(sum(len(s) for _, s in segments) / 16000, 3),
                  audio_seconds=round(len(samples) / 16000, 3))
elif stage == "moonshine":
    samples, rate = recording()
    folder = models + "/moonshine-tiny/"
    recognizer = sherpa.OfflineRecognizer(folder + "encoder_model.ort", folder + "decoder_model_merged.ort",
                                          folder + "tokens.txt", library=library)
    loaded = time.perf_counter()
    report.update(text=recognizer.transcribe(samples, rate), load_s_emulated=round(loaded - start, 3),
                  audio_seconds=round(len(samples) / rate, 3), sample_rate=rate)
    start = loaded
elif stage == "piper":
    from mind import phonemize

    folder = Path(models) / "piper"
    data = folder / "espeak-ng-data"
    phonemizer = phonemize.for_voice(data, folder / "en_GB-alba-medium.onnx.json")
    tts = sherpa.OfflineTts(folder / "en_GB-alba-medium.onnx", folder / "tokens.txt", data,
                           library=library, phonemizer=phonemizer)
    try:
        loaded = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="being-piper-smoke-") as scratch:
            seconds = tts.write(sys.argv[3], Path(scratch) / "speech.wav")
        report.update(audio_seconds=round(seconds, 3), sample_rate=tts.sample_rate,
                      load_s_emulated=round(loaded - start, 3))
        start = loaded
    finally:
        tts.close()
report["compute_s_emulated"] = round(time.perf_counter() - start, 3)
report["runtime_paths"] = runtime_paths()
print("SMOKE " + json.dumps(report), flush=True)
'''


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified(path, entry):
    return path.is_file() and path.stat().st_size == entry["bytes"] and file_sha256(path) == entry["sha256"]


def check_manifest(manifest, downloads=None):
    """Every asset's pin must match the download record, when there is one."""
    if downloads is None or not downloads.is_file():
        return
    recorded = {item["name"]: item for item in json.loads(downloads.read_text())["files"]}
    for entry in manifest["assets"]:
        item = recorded.get(entry["path"])
        if item is None or (item["sha256"], item["size_bytes"]) != (entry["sha256"], entry["bytes"]):
            raise ValueError(f"{entry['path']} is not pinned the same as in {downloads}")


def _safe(relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe path {relative!r}")
    return Path(*path.parts)


def _copy_member(archive, member, destination):
    if not member.isfile():
        raise ValueError(f"expected a regular file for {member.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with archive.extractfile(member) as source, destination.open("wb") as target:
        shutil.copyfileobj(source, target)


def prepare_media(assets, media, manifest):
    """Stage the verified runtime, models and recording into `media`."""
    assets, media = Path(assets), Path(media)
    for entry in manifest["assets"]:
        source = assets / _safe(entry["path"])
        if not verified(source, entry):
            raise ValueError(f"missing or unverified asset {entry['path']}")
        if "copy" in entry:
            destination = media / _safe(entry["copy"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            continue
        prefix = entry["prefix"]
        with tarfile.open(source, "r:bz2") as archive:
            members = {member.name: member for member in archive.getmembers()}
            for inner, target in entry.get("extract", {}).items():
                member = members.get(posixpath.join(prefix, inner))
                if member is None:
                    raise ValueError(f"{entry['path']} has no {inner}")
                _copy_member(archive, member, media / _safe(target))
            for inner, target in entry.get("extract_tree", {}).items():
                root = posixpath.join(prefix, inner) + "/"
                found = False
                for name, member in members.items():
                    if not name.startswith(root):
                        continue
                    found = True
                    relative = _safe(name[len(root):])
                    if member.isdir():
                        (media / _safe(target) / relative).mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        _copy_member(archive, member, media / _safe(target) / relative)
                    else:
                        raise ValueError(f"{name} is neither a file nor a folder")
                if not found:
                    raise ValueError(f"{entry['path']} has no {inner}/")
    package = media / "py/mind"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("")
    shutil.copyfile(BINDINGS, package / "sherpa.py")
    shutil.copyfile(PHONEMIZER, package / "phonemize.py")
    (media / "py/guest.py").write_text(GUEST_SCRIPT)
    return media


def guest_command(stage):
    command = (f"cd /tmp && PYTHONPATH={GUEST_MEDIA}/py PYTHONDONTWRITEBYTECODE=1 "
               f"python3 {GUEST_MEDIA}/py/guest.py {GUEST_MEDIA} {stage}")
    return command + (f" '{SENTENCE}'" if stage == "piper" else "")


def judge(stage, status, output):
    """The stage's verdict from its exit status and SMOKE line."""
    result = {"stage": stage, "status": status, "sigill": status == SIGILL_STATUS}
    lines = [line[len(MARK):] for line in output.splitlines() if line.startswith(MARK)]
    if status or len(lines) != 1:
        result.update(passed=False, log=output[-4000:])
        return result
    report = json.loads(lines[0])
    result.update(report)
    expected = f"{GUEST_MEDIA}/runtime/"
    paths = report.get("runtime_paths", [])
    loaded_here = bool(paths) and all(posixpath.normpath(path).startswith(expected) for path in paths)
    ok = {"vad": lambda: report.get("segments", 0) >= 1,
          "moonshine": lambda: bool(report.get("text", "").strip()),
          "piper": lambda: report.get("audio_seconds", 0) > 0}[stage]()
    result["passed"] = bool(ok and loaded_here)
    if not loaded_here:
        result["error"] = "the guest loaded a different sherpa-onnx or ONNX Runtime"
    return result


def use_runtime(media, runtime):
    """Replace the staged runtime libraries with the two files in `runtime`."""
    runtime = Path(runtime)
    for name in RUNTIME_FILES:
        source = runtime / name
        if not source.is_file():
            raise ValueError(f"{source} is missing")
        shutil.copyfile(source, Path(media) / "runtime" / name)


def shippable_verdict(report):
    """What the image may ship: the stages' outcome and the runtime pins, no host paths or logs."""
    stages = [{"stage": stage.get("stage"), "passed": stage.get("passed") is True,
               "sigill": stage.get("sigill") is True} for stage in report.get("stages", [])]
    versions = next((stage["versions"] for stage in report.get("stages", [])
                     if isinstance(stage.get("versions"), dict)), None)
    return {"version": VERDICT_VERSION, "source": "qemu-ivybridge", "qemu_cpu": report.get("qemu_cpu"),
            "passed": report.get("passed") is True and len(stages) == len(STAGES),
            "sigill": any(stage["sigill"] for stage in stages), "stages": stages,
            "runtime_sha256": dict(report.get("runtime_sha256") or {}), "versions": versions}


def output(vm, command, timeout=30):
    status, result = vm.run(command, timeout)
    if status:
        raise RuntimeError(f"guest command failed with exit code {status}\n{result}")
    return result


def check_cpu(vm):
    arch = output(vm, "uname -m")
    line = output(vm, "awk '/^flags[[:space:]]*:/ {print; exit}' /proc/cpuinfo")
    flags = set(line.partition(":")[2].split())
    if arch != "x86_64" or not REQUIRED <= flags or EXCLUDED & flags:
        raise RuntimeError(f"guest did not expose the required Ivy Bridge profile\n{arch}\n{line}")
    return {"arch": arch, "flags": sorted(flags)}


def checkpoint(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)


def expired(_signum, _frame):
    raise TimeoutError("Ivy Bridge speech smoke exceeded its host budget")


def terminated(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--assets", type=Path, default=ASSETS)
    parser.add_argument("--image", type=Path, default=ROOT / "out/images/beingos-ivybridge.img")
    parser.add_argument("--output", type=Path, default=ROOT / "out/sherpa-avx/ivybridge-smoke.json")
    parser.add_argument("--verdict", type=Path, default=ROOT / "out/sherpa-avx/sherpa-avx-smoke.json",
                        help="the shippable verdict (default: %(default)s)")
    parser.add_argument("--runtime", type=Path, metavar="DIR",
                        help="test the two runtime libraries in DIR instead of the release archive's")
    parser.add_argument("--budget", type=int, default=1200)
    parser.add_argument("--stage-timeout", type=int, default=420)
    parser.add_argument("--prepare-only", type=Path, metavar="DIR",
                        help="stage and verify the media into DIR without booting a VM")
    parser.add_argument("--fetch-only", action="store_true",
                        help="verify the pinned assets in out/voice (nothing is downloaded)")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    check_manifest(manifest, args.assets / "DOWNLOADS.json")
    if args.fetch_only:
        bad = [entry["path"] for entry in manifest["assets"] if not verified(args.assets / entry["path"], entry)]
        if bad:
            raise SystemExit("missing or unverified: " + ", ".join(bad))
        print("All pinned speech assets are present and verified.")
        return
    if args.prepare_only:
        prepare_media(args.assets, args.prepare_only, manifest)
        if args.runtime:
            use_runtime(args.prepare_only, args.runtime)
        print(f"Staged the speech smoke media in {args.prepare_only}")
        return
    if args.budget < 1 or args.stage_timeout < 1 or not args.image.is_file():
        parser.error("an existing image and positive time limits are required")
    report = {"scope": manifest["scope"], "image": str(args.image.resolve()),
              "image_sha256": file_sha256(args.image), "provenance": manifest, "qemu_cpu": "IvyBridge",
              "stages": [], "passed": False}
    with tempfile.TemporaryDirectory(prefix="being-sherpa-media-", dir="/tmp") as folder:
        media = prepare_media(args.assets, Path(folder), manifest)
        if args.runtime:
            use_runtime(media, args.runtime)
            report["runtime_source"] = "given"
        report["runtime_sha256"] = {name: file_sha256(media / "runtime" / name) for name in RUNTIME_FILES}
        vm = VM(image=args.image, update_media=f"fat:ro:{media}", memory="2G", threads="single")
        signal.signal(signal.SIGALRM, expired)
        signal.signal(signal.SIGTERM, terminated)
        signal.setitimer(signal.ITIMER_REAL, args.budget)
        try:
            vm.start()
            vm.login(timeout=120)
            output(vm, '(for device in /sys/class/net/*; do iface=${device##*/}; '
                   '[ "$iface" = lo ] || ip link set "$iface" down || exit 1; done)')
            output(vm, "systemctl stop --no-block being-mind being-cortex being-voice")
            report["guest_cpu"] = check_cpu(vm)
            output(vm, f"mount -t vfat -o ro /dev/vda1 {GUEST_MEDIA}")
            guest = output(vm, f"cd {GUEST_MEDIA}/runtime && sha256sum libsherpa-onnx-c-api.so libonnxruntime.so",
                           timeout=60).splitlines()
            if {name: digest for digest, name in (line.split() for line in guest)} != report["runtime_sha256"]:
                raise RuntimeError("the guest sees different sherpa-onnx or ONNX Runtime bytes")
            checkpoint(args.output, report)
            print("Verified Ivy Bridge flags and the exact runtime bytes", flush=True)
            for stage in STAGES:
                status, log = vm.run(guest_command(stage), args.stage_timeout)
                result = judge(stage, status, log)
                report["stages"].append(result)
                checkpoint(args.output, report)
                outcome = "SIGILL" if result["sigill"] else "passed" if result["passed"] else "failed"
                print(f"{stage}: {outcome}" + (f" ({result['text']!r})" if result.get("text") else ""), flush=True)
            report["sigill"] = any(stage["sigill"] for stage in report["stages"])
            report["passed"] = all(stage["passed"] for stage in report["stages"])
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            vm.destroy()
            checkpoint(args.output, report)
            checkpoint(args.verdict, shippable_verdict(report))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Exercise pinned sherpa-onnx streaming-model graphs on an Ivy Bridge guest."""

import argparse
import hashlib
import json
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
from pathlib import Path

from lab.vm import ROOT, VM


ASSETS = ROOT / "out/sherpa-avx"
MANIFEST = Path(__file__).with_name("sherpa_avx_assets.json")
GUEST_MEDIA = "/mnt/updates"
RELEASE = "sherpa-onnx-v2.0.2-linux-x64-shared-no-tts"
MODELS = {"fp32": "", "int8-encoder-joiner": ".int8"}
REQUIRED = {"avx", "f16c", "sse4_2"}
EXCLUDED = {"avx2", "fma", "bmi1", "bmi2", "avx512f"}


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified(path, entry):
    return path.is_file() and path.stat().st_size == entry["bytes"] and file_sha256(path) == entry["sha256"]


def fetch_assets(assets, manifest):
    for entry in manifest["assets"]:
        path = assets / entry["path"]
        if verified(path, entry):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as download:
            partial = Path(download.name)
        try:
            subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--max-time", "120",
                            "--output", partial, entry["url"]], check=True)
            if not verified(partial, entry):
                raise ValueError(f"unverified download for {path}")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)


def prepare_media(assets, media, manifest):
    archive_name = RELEASE + ".tar.bz2"
    for entry in manifest["assets"]:
        relative = Path(entry["path"])
        source = assets / relative
        if not verified(source, entry):
            raise ValueError(f"unverified fixture {relative}")
        if str(relative) != archive_name:
            destination = media / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

    with tarfile.open(assets / archive_name, "r:bz2") as archive:
        for relative in ("bin/sherpa-onnx", "lib/libonnxruntime.so"):
            member = archive.getmember(f"{RELEASE}/{relative}")
            if not member.isfile():
                raise ValueError(f"expected a regular file for {relative}")
            destination = media / "runtime" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            destination.chmod(0o755)


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


def recognize(vm, mode, timeout):
    suffix = MODELS[mode]
    runtime = f"{GUEST_MEDIA}/runtime"
    model_dir = f"{GUEST_MEDIA}/models"
    command = ["env", f"LD_LIBRARY_PATH={runtime}/lib", f"{runtime}/bin/sherpa-onnx",
               "--num-threads=1", "--provider=cpu", "--enable-endpoint=false",
               f"--tokens={model_dir}/tokens.txt",
               f"--encoder={model_dir}/encoder-epoch-99-avg-1{suffix}.onnx",
               f"--decoder={model_dir}/decoder-epoch-99-avg-1.onnx",
               f"--joiner={model_dir}/joiner-epoch-99-avg-1{suffix}.onnx",
               f"{GUEST_MEDIA}/fixtures/0.wav"]
    log = output(vm, shlex.join(command), timeout=timeout)
    load = re.search(r"Recognizer created in ([\d.]+) s", log)
    texts = []
    for line in log.splitlines():
        if line.strip().startswith("{"):
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if isinstance(result, dict) and isinstance(result.get("text"), str):
                texts.append(result["text"])
    if load is None or len(texts) != 1 or "yellow lamps" not in texts[0].casefold():
        raise RuntimeError(f"{mode}: missing recognizer initialization or expected decoded speech\n{log}")
    return {"configuration": mode, "load_s_emulated": float(load[1]), "transcript": texts[0], "log": log}


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=ASSETS)
    parser.add_argument("--image", type=Path, default=ROOT / "out/images/beingos-ivybridge.img")
    parser.add_argument("--output", type=Path, default=ASSETS / "ivybridge-smoke.json")
    parser.add_argument("--budget", type=int, default=420)
    parser.add_argument("--decode-timeout", type=int, default=135)
    parser.add_argument("--fetch-only", action="store_true", help="stage and verify the pinned public assets")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    if args.fetch_only:
        fetch_assets(args.assets, manifest)
        return
    if args.budget < 1 or args.decode_timeout < 1 or not args.image.is_file():
        parser.error("an existing image and positive time limits are required")
    report = {"scope": "Ivy Bridge TCG compatibility, CPU-only upstream WAV CLI, no audio capture",
              "image": str(args.image.resolve()), "image_sha256": file_sha256(args.image),
              "provenance": manifest, "qemu_cpu": "IvyBridge", "tests": [], "passed": False}
    with tempfile.TemporaryDirectory(prefix="being-sherpa-media-", dir="/tmp") as folder:
        media = Path(folder)
        prepare_media(args.assets, media, manifest)
        runtime = media / "runtime"
        report["elf_sha256"] = {str(path.relative_to(runtime)): file_sha256(path)
                                 for path in (runtime / "bin/sherpa-onnx", runtime / "lib/libonnxruntime.so")}
        vm = VM(image=args.image, update_media=f"fat:ro:{media}", memory="2G", threads="single")
        signal.signal(signal.SIGALRM, expired)
        signal.signal(signal.SIGTERM, terminated)
        signal.setitimer(signal.ITIMER_REAL, args.budget)
        try:
            vm.start()
            vm.login(timeout=120)
            output(vm, '(for device in /sys/class/net/*; do iface=${device##*/}; '
                   '[ "$iface" = lo ] || ip link set "$iface" down || exit 1; done)')
            output(vm, "systemctl stop --no-block being-mind being-cortex")
            report["guest_cpu"] = check_cpu(vm)
            output(vm, f"mount -t vfat -o ro /dev/vda1 {GUEST_MEDIA}")
            report["guest_elf_sha256"] = output(
                vm, f"cd {GUEST_MEDIA}/runtime && sha256sum bin/sherpa-onnx lib/libonnxruntime.so", timeout=50
            ).splitlines()
            if {path: digest for digest, path in (line.split() for line in report["guest_elf_sha256"])} != report["elf_sha256"]:
                raise RuntimeError("guest loaded different executable or ONNX Runtime bytes")
            report["loader"] = output(
                vm, f"LD_LIBRARY_PATH={GUEST_MEDIA}/runtime/lib /lib64/ld-linux-x86-64.so.2 "
                    f"--list {GUEST_MEDIA}/runtime/bin/sherpa-onnx"
            )
            library = re.search(r"^\s*libonnxruntime\.so\s*=>\s*(/\S+)", report["loader"], re.MULTILINE)
            expected = f"{GUEST_MEDIA}/runtime/lib/libonnxruntime.so"
            if library is None or posixpath.normpath(library[1]) != expected:
                raise RuntimeError("loader did not select the pinned ONNX Runtime library")
            checkpoint(args.output, report)
            print("Verified Ivy Bridge flags, exact executable, and loaded ONNX Runtime", flush=True)
            for mode in MODELS:
                result = recognize(vm, mode, args.decode_timeout)
                report["tests"].append(result)
                checkpoint(args.output, report)
                print(f"{mode}: {result['transcript']}", flush=True)
            report["passed"] = True
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            vm.destroy()
            checkpoint(args.output, report)


if __name__ == "__main__":
    main()

"""BEING_TRY_BENCH reaches try-image.sh through lab/build.sh's builder plumbing.

lab/build.sh runs try-image.sh inside a container, and the container sees
only the variables build.sh hands it. These tests run the real build.sh with
stand-in colima and docker commands. The stand-in docker runs only the
try-image.sh step, on the host with /src and /work mapped into a scratch
tree, and the try-image.sh it runs is the real script's option handling.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
root = os.environ["FAKE_ROOT"]
args = sys.argv[1:]
with open(os.path.join(root, "docker-calls.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:1] == ["--context"]:
    args = args[2:]
if args[:1] != ["run"]:
    sys.exit(0)
args = args[1:]
env = {}
while args and args[0] != "beingos-builder":
    option = args.pop(0)
    if option == "-e":
        key, _, value = args.pop(0).partition("=")
        env[key] = value
    elif option == "-v":
        args.pop(0)
command = args[1:]
if not any("try-image.sh" in part for part in command):
    sys.exit(0)
def mapped(part):
    return part.replace("/src/", root + "/").replace("/work/", root + "/work/")
command = [mapped(part) for part in command]
child = {key: value for key, value in os.environ.items() if not key.startswith("BEING_TRY_")}
child.update(env)
os.execvpe(command[0], command, child)
'''


def try_image_options():
    """The real try-image.sh up to where it starts building, then a record."""
    script = (ROOT / "os/board/beingos/try-image.sh").read_text()
    head, marker, _ = script.partition('. "$board_dir/slots.sh"\n')
    assert marker, "try-image.sh no longer sources slots.sh where expected"
    return head + '''mkdir -p "$output_dir"
printf '%s\\n' "$try_options" > "$output_dir/$output_name"
'''


class TryBenchPlumbing(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "tree"
        (self.root / "lab").mkdir(parents=True)
        shutil.copy2(ROOT / "lab/build.sh", self.root / "lab/build.sh")
        board = self.root / "os/board/beingos"
        board.mkdir(parents=True)
        (board / "try-image.sh").write_text(try_image_options())
        for tree in ("x86_64", "laptop", "laptop-maintenance"):
            images = self.root / "work/output" / tree / "images"
            (images / "efi-part/EFI/BOOT").mkdir(parents=True)
            (self.root / "work/output" / tree / "host").mkdir()
            for name in ("efi-part/EFI/BOOT/bootx64.efi", "rootfs.squashfs", "models.ext4"):
                (images / name).write_text("fixture")
        commands = self.root / "bin"
        commands.mkdir()
        (commands / "colima").write_text("#!/bin/sh\nexit 0\n")
        (commands / "docker").write_text(FAKE_DOCKER)
        for command in commands.iterdir():
            command.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("BEING_")}
        self.env.update(PATH=str(commands) + os.pathsep + os.environ["PATH"], FAKE_ROOT=str(self.root))

    def build(self, command, **env):
        return subprocess.run(["bash", "lab/build.sh", command], cwd=self.root, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def images(self):
        folder = self.root / "out/images"
        return {path.name: path.read_text().strip() for path in folder.glob("*.img")} if folder.exists() else {}

    def calls(self):
        log = self.root / "docker-calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_bench_stick_boots_bench_mode_under_its_own_name(self):
        for command, env, name, options in (
                ("try-image", {}, "beingos-try-bench.img", "being.bench=1"),
                ("try-screen", {}, "beingos-try-screen-bench.img", "being.screen=1 being.bench=1"),
                ("laptop-try", {}, "beingos-laptop-try-bench.img", "being.screen=1 being.bench=1"),
                ("laptop-try", {"BEING_LAPTOP_MAINTENANCE": "1"}, "beingos-laptop-maintenance-try-bench.img",
                 "being.screen=1 being.bench=1")):
            with self.subTest(command=command, env=env):
                result = self.build(command, BEING_TRY_BENCH="1", **env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.images().get(name), options, self.images())

    def test_ordinary_try_media_is_unchanged(self):
        for command, name, options in (("try-image", "beingos-try.img", ""),
                                       ("try-screen", "beingos-try-screen.img", "being.screen=1"),
                                       ("laptop-try", "beingos-laptop-try.img", "being.screen=1")):
            with self.subTest(command=command):
                result = self.build(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.images().get(name), options)
        self.assertFalse([name for name in self.images() if "bench" in name])

    def test_bench_quants_never_reach_the_image_build(self):
        # Only try-image.sh is told; Buildroot's post-image.sh (and so every
        # installed image and update bundle) never stages /models/bench.
        self.assertEqual(self.build("laptop-try", BEING_TRY_BENCH="1").returncode, 0)
        builds = [call for call in self.calls() if "make" in call]
        self.assertTrue(builds)
        for call in builds:
            self.assertFalse([part for part in call if "BEING_TRY_BENCH" in part], call)

    def test_a_bad_value_is_refused_before_building(self):
        result = self.build("try-image", BEING_TRY_BENCH="yes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("BEING_TRY_BENCH must be 0 or 1", result.stderr)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()

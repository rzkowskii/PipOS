"""Every mind module imports on a Python built without zlib, as the device's is.

Buildroot's python3 on the laptop has no zlib module, so a top-level
`import zlib` (or gzip, or zipfile compression) stops a whole organ at
start-up. Each module is imported in a fresh interpreter where zlib cannot be
imported at all.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIND = ROOT / "mind"

PROBE = r"""
import importlib, json, sys, traceback
sys.modules["zlib"] = None          # "import zlib" now raises ImportError
failed = {}
for name in json.loads(sys.argv[1]):
    try:
        importlib.import_module(name)
    except BaseException as error:  # SystemExit from an import counts too
        failed[name] = "".join(traceback.format_exception_only(type(error), error)).strip()
print(json.dumps(failed))
"""


def mind_modules():
    """mind and every mind/*.py except the entry point, as dotted names."""
    names = ["mind"]
    names += sorted(f"mind.{path.stem}" for path in MIND.glob("*.py") if path.stem not in ("__init__", "__main__"))
    return names


class ImportsWithoutZlib(unittest.TestCase):
    def test_the_probe_really_blocks_zlib(self):
        done = subprocess.run([sys.executable, "-I", "-c", PROBE, json.dumps(["zlib", "gzip"])],
                              capture_output=True, text=True, timeout=60, cwd=ROOT, check=True)
        self.assertEqual(set(json.loads(done.stdout)), {"zlib", "gzip"})

    def test_every_mind_module_imports_without_zlib(self):
        names = mind_modules()
        self.assertGreater(len(names), 50)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
        done = subprocess.run([sys.executable, "-c", PROBE, json.dumps(names)], capture_output=True, text=True,
                              timeout=300, cwd=ROOT, env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        failed = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(failed, {}, "these modules need zlib (or failed to import): "
                         + "; ".join(f"{name}: {why}" for name, why in failed.items()))


if __name__ == "__main__":
    unittest.main()

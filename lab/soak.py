"""Sample the organs of one factory image on a disposable Ivy Bridge guest.

Run in the foreground: python3 -m lab.soak --image out/images/beingos.img
    --seconds 300 --interval 30

The window starts after boot health and the first successful probe. Both host
and guest monotonic clocks must cover the requested duration. This is periodic organ
health evidence. Panel frame pacing, deliberate recovery and physical soaks
have separate exit criteria in the design.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time
from uuid import uuid4

from lab.candidate_manifest import fingerprint, regular
from lab.vm import ROOT, VM


ORGANS = ("brainstem", "face", "voice", "mind", "cortex")
BOOT_SECONDS = 480
PROBE_SECONDS = 30
MAX_SECONDS = 7 * 24 * 3600

# Run on the guest through its maintenance console. The boot-health check
# establishes initial readiness. Each subsequent probe needs a *new* heartbeat
# and a healthy state snapshot, plus a live cortex HTTP response.
GUEST_PROBE = """
import json, socket, time
from pathlib import Path
from mind.boot_health import booted_slot, cortex_ready
from mind.hardware_metrics import read_services

with socket.socket(socket.AF_UNIX) as bus:
    bus.settimeout(5)
    bus.connect('/run/being/bus.sock')
    bus.sendall(b'{"t":"hello","name":"soak","sub":["state.changed","stem.beat"]}\\n')
    state = beat = None
    with bus.makefile('r') as events:
        for line in events:
            event = json.loads(line)
            if event['t'] == 'state.changed':
                state = event
            if event['t'] == 'stem.beat':
                beat = event
            if state is not None and beat is not None:
                break
    if not state or not beat:
        raise RuntimeError('brainstem disconnected during health probe')
print(json.dumps({
    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'guest_seconds': time.monotonic(),
    'slot': booted_slot(),
    'confirmed': Path('/run/being-slot-good').read_text().strip(),
    'state': state['state'], 'unwell': state['unwell'], 'heartbeat': beat['state'],
    'cortex_ready': cortex_ready(), 'services': read_services(Path('/proc')),
}))
"""


@contextmanager
def deadline(seconds):
    def expired(signum, frame):
        raise TimeoutError(f"exceeded {seconds}s deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def sample(vm):
    status, output = vm.run("PYTHONPATH=/usr/lib/being python3 /run/soak-probe.py", PROBE_SECONDS)
    if status:
        raise RuntimeError(f"guest probe exited {status}: {output[-1000:]}")
    return json.loads(output)


def health_errors(current, previous):
    errors = []
    if (not current["boot_id"] or current["slot"] not in ("A", "B")
            or current["confirmed"] != current["slot"]):
        errors.append("invalid boot identity or unconfirmed slot")
    if (not current["heartbeat"] or current["state"] == "waking"
            or current["unwell"] is not False or current["cortex_ready"] is not True):
        errors.append("brainstem or cortex is unhealthy")
    if previous:
        if (current["boot_id"], current["slot"]) != (previous["boot_id"], previous["slot"]):
            errors.append("guest rebooted or changed slot")
        if current["guest_seconds"] <= previous["guest_seconds"]:
            errors.append("guest monotonic clock did not advance")
    for organ in ORGANS:
        unit = f"being-{organ}.service"
        now = current["services"].get(unit, {})
        active_since = now.get("active_since_boot_us")
        if (now.get("active_state") != "active" or now.get("sub_state") != "running"
                or now.get("restarts") != 0 or not active_since):
            errors.append(f"{unit} inactive, restarted or missing counters")
        elif previous and active_since != previous["services"][unit]["active_since_boot_us"]:
            errors.append(f"{unit} changed activation")
    return errors


def observe(vm, seconds, interval, record, *, clock=time.monotonic, sleep=time.sleep):
    previous = None
    while True:
        probe_start = clock()
        with deadline(PROBE_SECONDS):
            current = sample(vm)
        now = clock()
        if previous is None:
            start, guest_start = now, current["guest_seconds"]
        errors = health_errors(current, previous)
        if now - probe_start > PROBE_SECONDS:
            errors.append("guest exceeded the probe deadline")
        if previous is not None and now - last > interval + PROBE_SECONDS:
            errors.append("missing observations during the requested window")
        elapsed = now - start
        guest_elapsed = current["guest_seconds"] - guest_start
        if guest_elapsed < seconds and elapsed >= seconds + interval + PROBE_SECONDS:
            errors.append("guest never observed the requested duration within the host budget")
        record("sample", observed_seconds=elapsed, guest_observed_seconds=guest_elapsed, probe_seconds=now - probe_start,
               health=current, errors=errors)
        if errors:
            raise RuntimeError(", ".join(errors))
        remaining = seconds - min(elapsed, guest_elapsed)
        if previous is not None and remaining <= 0:
            return elapsed
        previous, last = current, now
        sleep(min(interval, remaining))


def run(image, seconds, interval, report):
    def record(event, **values):
        line = {"event": event, "utc": datetime.now(timezone.utc).isoformat(), **values}
        print(json.dumps(line, separators=(",", ":"), allow_nan=False), file=report, flush=True)

    vm = VM(image=image, loopback=True)
    record("starting", image=str(image), requested_seconds=seconds, interval_seconds=interval)
    try:
        with regular(image) as (stream, info):
            try:
                with deadline(BOOT_SECONDS):
                    record("image", **fingerprint(stream, info.st_size))
                    vm.start()
                    vm.login(180)
                    status, _ = vm.run(
                        'case " $(cat /proc/cmdline) " in *" being.try="*|*" being.installed="*) false ;; '
                        '*" rauc.slot=A "*|*" rauc.slot=B "*) true ;; *) false ;; esac'
                    )
                    if status:
                        raise RuntimeError("supply an A/B factory image")
                    vm.put("/run/soak-probe.py", GUEST_PROBE)
                    status, _ = vm.run("while [ ! -s /run/being-slot-good ]; do sleep 1; done", timeout=240)
                    if status:
                        raise RuntimeError("factory boot health did not confirm the slot")
                    record("booted")
                elapsed = observe(vm, seconds, interval, record)
            finally:
                # The overlay is discarded. Killing this QEMU directly also
                # works if its QMP channel stalled during start or observation.
                directory = vm.dir
                try:
                    vm.power_cut()
                finally:
                    vm.destroy()
                if vm.running() or (directory is not None and directory.exists()):
                    raise RuntimeError("could not remove this guest or its overlay")
                record("cleaned")
    except BaseException as error:
        record("failed", reason=str(error) or type(error).__name__)
        raise
    record("passed", observed_seconds=elapsed)
    return elapsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, type=Path, help="raw factory image, opened read-only")
    parser.add_argument("--seconds", required=True, type=int, help="observation window after readiness, up to one week")
    parser.add_argument("--interval", type=int, default=60, help="seconds between checks (10–300, default 60)")
    parser.add_argument("--report", type=Path, help="new JSONL file (default out/logs/soak-<run>.jsonl)")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= MAX_SECONDS or not 10 <= args.interval <= 300:
        parser.error("use a duration from 1 second to one week and an interval from 10 to 300 seconds")
    image = args.image.expanduser().absolute()
    if not image.is_file() or image.is_symlink():
        parser.error("--image must name a regular factory image")
    path = args.report or ROOT / "out" / "logs" / f"soak-{uuid4().hex}.jsonl"

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"received {signal.Signals(signum).name}")

    previous = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, interrupted)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as report:
            print(f"Writing soak evidence to {path}", flush=True)
            elapsed = run(image, args.seconds, args.interval, report)
        print(f"Observed {elapsed:.1f} healthy seconds. Guest removed.")
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"Soak failed: {error or type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())

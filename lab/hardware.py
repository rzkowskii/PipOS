"""Collect and compare PipOS hardware measurements over pinned LAN SSH."""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


RUNS = Path(__file__).resolve().parents[1] / "out" / "hardware" / "runs"
SCHEMA_VERSION = 1
METRICS_PORT = 2222
DEV_PORT = 2223
MEASUREMENTS = (
    ("Elapsed", "elapsed_seconds", "s"),
    ("CPU busy", "cpu_busy_percent", "%"),
    ("CPU I/O wait", "cpu_iowait_percent", "%"),
    ("Minimum available memory", "memory_available_min_kib", "KiB"),
    ("Mean CPU frequency", "cpu_frequency_mean_khz", "kHz"),
    ("Peak temperature", "temperature_max_c", "°C"),
    ("Peak CPU temperature", "cpu_temperature_max_c", "°C"),
    ("Max core throttle events", "cpu_core_throttle_max_events", "events"),
    ("Peak fan speed", "fan_max_rpm", "rpm"),
    ("Mean CPU package power", "package_power_mean_watts", "W"),
    ("Mean battery discharge", "battery_discharge_mean_watts", "W"),
    ("Mean Wi-Fi signal", "wifi_signal_mean_dbm", "dBm"),
    ("Wi-Fi connected samples", "wifi_connected_percent", "%"),
)
IO_MEASUREMENTS = {
    "network": (("receive", "rx_bytes", 1024, "KiB/s"),
                ("send", "tx_bytes", 1024, "KiB/s")),
    "disk": (("read", "read_bytes", 1024, "KiB/s"),
             ("write", "write_bytes", 1024, "KiB/s"),
             ("busy", "busy_ms", 10, "%")),
}


def lan_host(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        hostname = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}[.]?", value)
        if hostname and all(label and len(label) <= 63 and
                            not label.startswith("-") and not label.endswith("-")
                            for label in value.rstrip(".").split(".")):
            return value
    raise argparse.ArgumentTypeError("use a hostname or IP address for the laptop")


def run_label(value):
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        return value
    raise argparse.ArgumentTypeError("use a 1–64 character label with letters, numbers, dots, underscores or hyphens")


def integer_between(minimum, maximum):
    def parse(value):
        try:
            result = int(value)
        except ValueError:
            result = minimum - 1
        if minimum <= result <= maximum:
            return result
        raise argparse.ArgumentTypeError(f"use an integer from {minimum} to {maximum}")
    return parse


def ssh_file(path):
    path = path.expanduser().absolute()
    if not path.is_file():
        raise ValueError(f"SSH file does not exist: {path}")
    if any(not character.isprintable() or character in '"\\$%' for character in str(path)):
        raise ValueError(f"unsupported character in SSH file path: {path}")
    return str(path)


def ssh_command(args, user, *, tty=False):
    identity = ssh_file(args.key)
    known_hosts = ssh_file(args.known_hosts)
    return [
        "ssh", "-F", "/dev/null", "-tt" if tty else "-T",
        "-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
        "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
        "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
        "-o", f'UserKnownHostsFile="{known_hosts}"',
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
        "-i", identity, "-p", str(args.port), "-l", user, args.host,
    ]


def bounded_output(command, timeout):
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process, selectors.DefaultSelector() as streams:
        stdout, stderr = bytearray(), bytearray()
        streams.register(process.stdout, selectors.EVENT_READ, (stdout, 4 * 1024 * 1024))
        streams.register(process.stderr, selectors.EVENT_READ, (stderr, 64 * 1024))
        try:
            while streams.get_map():
                events = streams.select(max(0, deadline - time.monotonic()))
                if not events or time.monotonic() >= deadline:
                    raise TimeoutError(f"SSH collection exceeded {timeout} seconds")
                for key, _ in events:
                    buffer, limit = key.data
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        streams.unregister(key.fileobj)
                    else:
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise ValueError("SSH collection exceeded its output limit")
            if process.wait(timeout=max(0, deadline - time.monotonic())):
                detail = stderr.decode(errors="replace").strip()
                raise ValueError(f"SSH collection failed (exit {process.returncode}): {detail}")
        finally:
            if process.poll() is None:
                process.kill()
        return stdout


def validate_metrics(data):
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or
            data["schema_version"] != SCHEMA_VERSION):
        raise ValueError(f"expected hardware metrics schema version {SCHEMA_VERSION}")
    if (not isinstance(data.get("captured_at"), str) or
            not isinstance(data.get("system"), dict) or
            not isinstance(data.get("sampling"), dict) or
            not isinstance(data.get("services"), dict) or
            not isinstance(data.get("window"), dict) or
            not isinstance(data.get("samples"), list) or not data["samples"]):
        raise ValueError("hardware metrics response is incomplete")
    sampling = data["sampling"]
    if set(sampling) != {"seconds", "interval"} or any(type(value) is not int for value in sampling.values()):
        raise ValueError("hardware metrics sampling settings are invalid")
    for family in IO_MEASUREMENTS:
        counters = data["window"].get(family, {})
        if not isinstance(counters, dict) or any(not isinstance(device, dict) for device in counters.values()):
            raise ValueError(f"hardware metrics {family} counters are invalid")
    return data


def save_run(directory, run):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = directory.lstat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError(f"hardware output directory must be private and owned by this user: {directory}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = directory / f"{stamp}-{run['scenario']}-{uuid4().hex[:12]}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".capture-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(run, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def collect(args):
    command = ssh_command(args, "being-observe")
    command += [f"metrics --seconds {args.seconds} --interval {args.interval}"]
    data = validate_metrics(json.loads(bounded_output(command, args.seconds + 20)))
    if data["sampling"] != {"seconds": args.seconds, "interval": args.interval}:
        raise ValueError("the device did not use the requested sampling settings")
    run = {
        "target": args.host,
        "scenario": args.scenario,
        "variant": args.variant,
        "requested_seconds": args.seconds,
        "requested_interval_seconds": args.interval,
        "metrics": data,
    }
    print(save_run(RUNS, run))
    return 0


def read_run(path):
    with path.open(encoding="utf-8") as handle:
        run = json.load(handle)
    if not isinstance(run, dict) or not isinstance(run.get("scenario"), str):
        raise ValueError(f"expected a capture produced by collect: {path}")
    validate_metrics(run.get("metrics"))
    return run


def number(value):
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def measurement(label, first, second, unit):
    first, second = number(first), number(second)
    quantities = ["—" if value is None else f"{value:,.2f} {unit}" for value in (first, second)]
    delta_unit = {"%": "pp", "dBm": "dB"}.get(unit, unit)
    delta = "—" if None in (first, second) else f"{second - first:+,.2f} {delta_unit}"
    print(f"{label:28} {quantities[0]:>16} {quantities[1]:>16} {delta:>16}")


def rate(window, family, device, field, scale):
    elapsed = number(window.get("elapsed_seconds"))
    value = number(window.get(family, {}).get(device, {}).get(field))
    if elapsed and elapsed > 0 and value is not None:
        return value / elapsed / scale
    return None


def compare(args):
    before = read_run(args.baseline)
    after = read_run(args.candidate)
    if before["scenario"] != after["scenario"]:
        raise ValueError(f"scenario mismatch: {before['scenario']} vs {after['scenario']}")
    print(f"Scenario {before['scenario']}")
    for label, run in (("Baseline", before), ("Candidate", after)):
        request = f"{run.get('requested_seconds')} s / {run.get('requested_interval_seconds')} s interval"
        print(f"{label} {run.get('variant') or '(unlabelled)'} on {run.get('target')} ({request})")
        system = run["metrics"]["system"]
        provenance = [f"{name} {system[field]}" for name, field in
                      (("build", "build_id"), ("kernel", "kernel"), ("slot", "slot")) if system.get(field)]
        digest = system.get("model_sha256")
        if isinstance(digest, str) and digest:
            provenance.append(f"model SHA-256 {digest[:12]}")
        cortex = run["metrics"]["services"].get("start", {}).get("being-cortex.service", {}).get("arguments", {})
        if "threads" in cortex and "prompt_threads" in cortex:
            provenance.append(f"cortex threads {cortex['threads']} generation / {cortex['prompt_threads']} prompt")
        if provenance:
            print("  " + ", ".join(provenance))
    print(f"{'Measurement':28} {'Baseline':>16} {'Candidate':>16} {'Change':>16}")
    windows = [run["metrics"]["window"] for run in (before, after)]
    for label, field, unit in MEASUREMENTS:
        first, second = [window.get(field) for window in windows]
        measurement(label, first, second, unit)
    for family, fields in IO_MEASUREMENTS.items():
        devices = sorted(set(windows[0].get(family, {})) | set(windows[1].get(family, {})))
        for device in devices:
            for label, field, scale, unit in fields:
                first, second = [rate(window, family, device, field, scale) for window in windows]
                measurement(f"{device} {label}", first, second, unit)
    return 0


def dev(args, remote):
    interactive = not remote and sys.stdin.isatty()
    if not remote and not interactive:
        raise ValueError("open dev in a terminal or supply a remote command after --")
    command = ssh_command(args, "root", tty=interactive)
    if remote:
        command.append(shlex.join(remote))
    return subprocess.run(command, check=False).returncode


def control_dev(args):
    command = ssh_command(args, "root")
    command.append("dev-on" if args.mode == "open-dev" else "dev-off")
    response = bounded_output(command, 20).decode().strip()
    if response:
        print(response)
    return 0


def add_connection(parser, port):
    parser.add_argument("host", type=lan_host, help="laptop hostname or IP address")
    parser.add_argument("--key", type=Path, required=True, help="private SSH key file")
    parser.add_argument("--known-hosts", type=Path, required=True, help="file with verified host-key pins")
    parser.add_argument("--port", type=integer_between(1, 65535), default=port, help=f"SSH port (default {port})")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    remote = []
    if argv and argv[0] == "dev" and "--" in argv:
        separator = argv.index("--")
        argv, remote = argv[:separator], argv[separator + 1:]
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    capture = modes.add_parser("collect", help="capture bounded hardware metrics over the observation port")
    add_connection(capture, METRICS_PORT)
    capture.add_argument("--scenario", required=True, type=run_label, help="repeatable workload label")
    capture.add_argument("--variant", type=run_label, help="build or experiment label")
    capture.add_argument("--seconds", type=integer_between(0, 300), default=30, help="sampling length (default 30)")
    capture.add_argument("--interval", type=integer_between(1, 60), default=2, help="sample interval (default 2)")
    capture.set_defaults(action=collect)
    comparison = modes.add_parser("compare", help="compare two captures of the same scenario")
    comparison.add_argument("baseline", type=Path)
    comparison.add_argument("candidate", type=Path)
    comparison.set_defaults(action=compare)
    development = modes.add_parser("dev", help="open the separately enabled development SSH session",
                                   epilog="Add -- followed by a remote command and its arguments for a headless call.")
    add_connection(development, DEV_PORT)
    development.set_defaults(action=lambda args: dev(args, remote))
    for name, help_text in (("open-dev", "start the bounded development SSH window"),
                            ("close-dev", "stop the development SSH window and its connections")):
        control = modes.add_parser(name, help=help_text)
        add_connection(control, METRICS_PORT)
        control.set_defaults(action=control_dev)
    args = parser.parse_args(argv)
    try:
        return args.action(args)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"being hardware: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

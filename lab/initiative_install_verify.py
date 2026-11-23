"""Verify initiative on an already running disposable installed serial guest."""

import base64
import hashlib
import json
import re
import shlex
import time
from dataclasses import asdict
from uuid import uuid4

from mind.initiative import InitiativePolicy


GUEST_PATH = "/run/being-initiative-install-verify.py"
GUEST = r"""
import base64
import json
import os
import socket
import stat
import sys
import time
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path

from mind.initiative import load_policy, save_policy
from mind.reminders import Reminders
from mind.skills import Skills
from mind.storage import atomic_write

ROOT = Path('/mind')
POLICY = ROOT / 'initiative.json'
BOOT = Path('/proc/sys/kernel/random/boot_id')
NOW = Skills(ROOT).now


def snapshot():
    mode = POLICY.lstat()
    assert stat.S_ISREG(mode.st_mode), 'initiative policy is not a regular file'
    return {
        'installed': 'being.installed=1' in Path('/proc/cmdline').read_text().split(),
        'mind_mounted': ROOT.is_mount(),
        'boot': BOOT.read_text().strip(),
        'life': json.loads((ROOT / 'life.json').read_text())['id'],
        'mode': stat.S_IMODE(mode.st_mode),
        'owner': [mode.st_uid, mode.st_gid],
        'bytes': base64.b64encode(POLICY.read_bytes()).decode('ascii'),
        'policy': asdict(load_policy(ROOT)),
        'epoch': time.time(),
    }


def fixture_times():
    window = load_policy(ROOT).quiet_hours
    def minute(label):
        hours, minutes = map(int, label.split(':'))
        return hours * 60 + minutes
    start = minute(window.start)
    duration = (minute(window.end) - start) % 1440
    assert 20 <= duration <= 1420, 'fixture needs room in both quiet and allowed periods'
    day = NOW().replace(hour=0, minute=0, second=0, microsecond=0)
    middle = start + duration // 2
    return {name: (day + timedelta(minutes=minutes)).timestamp() for name, minutes in (
        ('create', middle - 5), ('due', middle + 5), ('allowed', start + duration + (1440 - duration) // 2),
    )}


def set_clock(epoch):
    time.clock_settime(time.CLOCK_REALTIME, epoch)
    now = NOW()
    return {'local_time': now.isoformat(), 'quiet': load_policy(ROOT).is_quiet(now)}


def observe(options):
    deadline = time.monotonic() + options.get('timeout', 25)
    topics = ['body.lid', 'state.changed', 'stem.beat', 'mind.announce',
              'stem.announce_ready', 'say.request', 'say.started', 'say.done']
    with socket.socket(socket.AF_UNIX) as bus:
        bus.settimeout(3)
        bus.connect('/run/being/bus.sock')
        hello = {'t': 'hello', 'name': 'initiative-install-verifier', 'sub': topics}
        bus.sendall((json.dumps(hello) + '\n').encode())
        pending = bytearray()

        def event(until):
            while b'\n' not in pending:
                remaining = until - time.monotonic()
                if remaining <= 0:
                    return None
                bus.settimeout(remaining)
                try:
                    chunk = bus.recv(65536)
                except TimeoutError:
                    return None
                if not chunk:
                    raise RuntimeError('installed bus disconnected')
                pending.extend(chunk)
            line, _, rest = pending.partition(b'\n')
            pending[:] = rest
            return json.loads(line)

        lid_open = ready = False
        while not (lid_open and ready):
            row = event(deadline)
            assert row is not None, 'installed voice did not become ready with an open lid'
            if row['t'] == 'body.lid':
                lid_open = row.get('closed') is False
            elif row['t'] == 'state.changed':
                ready = row['state'] in ('idle', 'attending', 'drowsy') and not row['unwell']

        if options.get('ready_only'):
            return {'ready': True}
        clock = set_clock(options['epoch'])
        assert clock['quiet'] is options['quiet'], clock
        if options.get('enable_reminders'):
            save_policy(ROOT, replace(load_policy(ROOT), reminders=True))
        until = deadline if not options['quiet'] else min(deadline, time.monotonic() + 3)
        evidence = {**clock, 'heartbeats': 0, 'started': 0, 'offers': [], 'grants': [], 'receipts': [], 'text': []}
        while (row := event(until)) is not None:
            topic = row['t']
            if topic == 'stem.beat':
                evidence['heartbeats'] += 1
            elif topic == 'mind.announce':
                evidence['offers'].append(row['announcement'])
            elif topic == 'stem.announce_ready':
                evidence['grants'].append(row['announcement'])
            elif topic == 'say.request' and row['text']:
                evidence['text'].append(row['text'])
            elif topic == 'say.started':
                evidence['started'] += 1
            elif topic == 'say.done' and row.get('final') and row.get('announcement'):
                evidence['receipts'].append(row['announcement'])
                if len(evidence['receipts']) == 1:
                    until = min(deadline, time.monotonic() + 3)
        return evidence


def reminders(action, payload):
    queue = Reminders(ROOT)
    try:
        if action == 'pending':
            return {'entry': next((asdict(entry) for entry in queue.matching(payload, kind='reminder')), None),
                    'epoch': time.time()}
        for task in payload:
            for entry in queue.matching(task, kind='reminder'):
                queue.cancel(entry.id, kind='reminder')
        return {'cleared': True}
    finally:
        queue.close()


action, payload = sys.argv[1], json.loads(sys.argv[2])
if action == 'snapshot':
    result = snapshot()
elif action == 'times':
    result = fixture_times()
elif action == 'set_clock':
    result = set_clock(payload)
elif action == 'observe':
    result = observe(payload)
elif action == 'restore_policy':
    atomic_write(POLICY, base64.b64decode(payload).decode('utf-8'))
    result = {'restored': True}
else:
    result = reminders(action, payload)
print(json.dumps(result))
"""


def _run(vm, command, timeout=30):
    status, output = vm.run(command, timeout)
    if status:
        raise AssertionError(f"installed initiative probe failed ({status}): {command}\n{output}")
    return output


def _guest(vm, action, payload=None, *, timeout=30):
    args = shlex.join((GUEST_PATH, action, json.dumps(payload)))
    command = f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/usr/lib/being python3 {args}"
    return json.loads(_run(vm, command, timeout))


def _verify_policy(guest, expected):
    assert guest["installed"] and guest["mind_mounted"], guest
    assert (guest["mode"], guest["owner"]) == (0o600, [0, 0]), guest
    assert guest["policy"] == asdict(expected), guest
    raw = base64.b64decode(guest["bytes"])
    assert json.loads(raw) == {"version": 1, **asdict(expected)}, guest
    return hashlib.sha256(raw).hexdigest()


def _stop_time_sync(vm):
    unit = "systemd-timesyncd.service"
    if vm.run(f"systemctl is-active --quiet {unit}", 15)[0] != 0:
        return False
    _run(vm, f"systemctl stop {unit}")
    return True


def _quiet_probe(vm, epoch, *, enable_reminders=False):
    result = _guest(vm, "observe", {"quiet": True, "epoch": epoch, "enable_reminders": enable_reminders})
    assert result["heartbeats"] >= 2, result
    assert not any(result[key] for key in ("offers", "grants", "receipts", "started", "text")), result
    return result


def _ready(vm, boot_timeout):
    patience = min(boot_timeout, 50)
    _guest(vm, "observe", {"ready_only": True, "timeout": patience}, timeout=patience + 5)


def _diagnostics(vm):
    units = ("being-mind.service", "being-brainstem.service", "being-lab-body.service", "being-face.service")
    status = "systemctl --no-pager --full status " + " ".join(units)
    journal = "journalctl -b --no-pager -o cat -n 60 " + " ".join(f"-u {unit}" for unit in units)
    _, output = vm.run(f"{{ {status} || true; }}; {journal}", 15)
    return output[-9500:]


def verify_installed_initiative(vm, expected: InitiativePolicy, *, boot_timeout=150, reply_timeout=30):
    """Return evidence from two installed boots on the caller's disposable overlay.

    The caller owns the VM and must already have logged in to its serial shell.
    This expects installed quiet hours and reminder announcements disabled so
    the submitted item stays pending while the machine reboots. The installed
    policy is left untouched through the reboot. Then a temporary policy enables
    reminder announcements, demonstrating quiet-hour suppression independently.
    """
    if expected.reminders or expected.quiet_hours is None:
        raise ValueError("use an installed policy with quiet hours and reminder announcements disabled")

    suffix = uuid4().hex[:6]
    remind_task = f"check the installer clock {suffix}"
    cancel_task = f"check the second clock {suffix}"
    vm.put(GUEST_PATH, GUEST)
    first = _guest(vm, "snapshot")
    original_digest = _verify_policy(first, expected)
    started = time.monotonic()
    times = _guest(vm, "times")
    restore_timesync = changed_policy = False
    phase = "before reboot"
    shell_ready = probe_ready = True
    failure = None

    def ask(question):
        return vm.converse(question, reply_timeout)

    try:
        restore_timesync = _stop_time_sync(vm)
        _ready(vm, boot_timeout)
        clock = _guest(vm, "set_clock", times["create"])
        assert clock["quiet"], clock
        replies = {"time": ask("What time is it?"), "date": ask("What's today's date?"),
                   "greeting": ask("Are you there?")}
        assert re.match(r"It's \d{1,2}:\d{2}\b", replies["time"]), replies
        assert re.match(r"It's \w+, \w+ \d{1,2}, \d{4}\.", replies["date"]), replies
        assert replies["greeting"].startswith(("I'm here.", "I'm right here.", "I can hear you.")), replies

        replies["set"] = ask(f"Remind me in one minute to {remind_task}")
        confirmation = re.match(r"Reminder (\d+) set\b", replies["set"])
        assert confirmation, replies
        reminder_id = int(confirmation[1])
        replies["set_cancelled_item"] = ask(f"Remind me in two minutes to {cancel_task}")
        cancellation = re.match(r"Reminder (\d+) set\b", replies["set_cancelled_item"])
        assert cancellation and int(cancellation[1]) != reminder_id, replies
        replies["cancel"] = ask(f"Cancel reminder {cancellation[1]}")
        assert replies["cancel"] == f"Cancelled reminder {cancellation[1]}.", replies
        replies["list"] = ask("What are my reminders?")
        assert remind_task in replies["list"] and cancel_task not in replies["list"], replies
        quiet_before = _quiet_probe(vm, times["due"])
        pending = _guest(vm, "pending", remind_task)
        assert pending["entry"]["id"] == reminder_id and pending["entry"]["due_at"] < pending["epoch"], pending
        assert _guest(vm, "snapshot")["bytes"] == first["bytes"], "policy changed before reboot"

        phase = "after reboot"
        shell_ready = probe_ready = False
        vm.send("systemctl reboot\n")
        vm.login(boot_timeout)
        shell_ready = True
        vm.select_try_from_screen(boot_timeout)
        vm.put(GUEST_PATH, GUEST)
        probe_ready = True
        second = _guest(vm, "snapshot")
        assert second["boot"] != first["boot"] and second["life"] == first["life"], (first, second)
        assert _verify_policy(second, expected) == original_digest and second["bytes"] == first["bytes"]
        restore_timesync = _stop_time_sync(vm)
        _ready(vm, boot_timeout)
        clock_after_reboot = _guest(vm, "set_clock", times["due"])
        assert clock_after_reboot["quiet"], clock_after_reboot
        assert _guest(vm, "pending", remind_task)["entry"] == pending["entry"], "pending reminder changed across reboot"
        post_reboot_replies = {"time": ask("What time is it?"), "date": ask("What's today's date?"),
                               "list": ask("What are my reminders?")}
        assert re.match(r"It's \d{1,2}:\d{2}\b", post_reboot_replies["time"]), post_reboot_replies
        assert post_reboot_replies["date"] == replies["date"], post_reboot_replies
        assert f"{remind_task}, overdue" in post_reboot_replies["list"], post_reboot_replies
        assert _guest(vm, "snapshot")["bytes"] == first["bytes"], "policy changed before optional reminder test"

        changed_policy = True
        quiet_enabled = _quiet_probe(vm, times["due"], enable_reminders=True)
        assert _guest(vm, "pending", remind_task)["entry"] == pending["entry"], "quiet hours consumed the pending reminder"
        delivered = _guest(vm, "observe", {"quiet": False, "epoch": times["allowed"], "timeout": 40}, timeout=45)
        assert len(delivered["offers"]) == 1, delivered
        assert delivered["offers"] == delivered["grants"] == delivered["receipts"], delivered
        assert delivered["started"] == 1 and remind_task in " ".join(delivered["text"]), delivered
        assert _guest(vm, "pending", remind_task)["entry"] is None, "confirmed reminder stayed pending"
        return {
            "policy": asdict(expected), "installed_policy_sha256": original_digest,
            "boot_ids": [first["boot"], second["boot"]], "same_life": first["life"] == second["life"],
            "foreground_replies": replies, "foreground_replies_after_reboot": post_reboot_replies,
            "quiet_clock_after_reboot": clock_after_reboot, "reminder_id": reminder_id,
            "quiet_with_reminders_disabled": quiet_before, "quiet_with_reminders_enabled": quiet_enabled,
            "delivery": delivered,
        }
    except Exception as error:
        failure = error
        error.add_note(f"installed initiative verification {phase}")
        if vm.running() and shell_ready:
            try:
                error.add_note(_diagnostics(vm))
            except Exception as diagnostic_error:
                error.add_note(f"service diagnostics failed: {diagnostic_error}\n{vm.output()[-4000:]}")
        else:
            error.add_note(vm.output()[-4000:])
        raise
    finally:
        if vm.running() and shell_ready and probe_ready:
            try:
                if changed_policy:
                    _guest(vm, "restore_policy", first["bytes"])
                    assert _verify_policy(_guest(vm, "snapshot"), expected) == original_digest
                _guest(vm, "clear", [remind_task, cancel_task])
                _guest(vm, "set_clock", first["epoch"] + time.monotonic() - started)
                if restore_timesync:
                    _run(vm, "systemctl start systemd-timesyncd.service")
                _run(vm, f"rm -f {GUEST_PATH}")
            except Exception as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"installed initiative fixture cleanup failed: {cleanup_error}")

"""The check-up: injected readings, at most three findings, safe repairs only."""

import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from mind import action_log, doctor

HEALTHY = {unit: {"Id": unit, "ActiveState": "active", "SubState": "running", "NRestarts": "0"}
           for unit in doctor.ORGANS}


class FakeSystem:
    def __init__(self, *, units=None, failed=(), disk=(120e9, 60e9, 60e9), cortex=True, uptime=7200.0,
                 checks=None, backup=None, restart_ok=True, audio_ok=True, continuity_warning=False):
        self._units = {**HEALTHY, **(units or {})} if units is not False else None
        self.failed = list(failed)
        self._disk = disk
        self.cortex = cortex
        self._uptime = uptime
        self.checks = checks or {}
        self.backup = backup
        self._continuity_warning = continuity_warning
        self.restart_ok, self.audio_ok = restart_ok, audio_ok
        self.restarted, self.audio_runs = [], 0
        self.budgets = []

    def units(self, names):
        return None if self._units is None else {name: self._units[name] for name in names if name in self._units}

    def failed_units(self):
        return self.failed

    def restart(self, unit):
        self.restarted.append(unit)
        return self.restart_ok

    def audio_init(self):
        self.audio_runs += 1
        return self.audio_ok

    def disk(self, path):
        return self._disk

    def quick_check(self, path, budget_s=None):
        self.budgets.append(budget_s)
        return self.checks.get(Path(path).name)

    def uptime(self):
        return self._uptime

    def cortex_ok(self):
        return self.cortex

    def last_backup(self, state_dir):
        return self.backup

    def continuity_warning(self):
        return self._continuity_warning


def unit(name, active="active", restarts=0):
    return {name: {"Id": name, "ActiveState": active, "SubState": "dead" if active != "active" else "running",
                   "NRestarts": str(restarts)}}


class Checks(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        doctor._repaired.clear()
        doctor._last_speed.clear()
        self.now = time.time()
        self.voice = {"floor_dbfs": -62, "heard_ago_s": 30}
        self.body = {"health_pct": 81, "cycles": 71, "temp": 48}

    def facts(self, **changes):
        values = {"state_dir": self.mind, "body": self.body, "voice": self.voice, "now": self.now}
        values.update(changes)
        return doctor.Facts(**values)

    def examine(self, system, **changes):
        findings, repairs = doctor.examine(system, self.facts(**changes))
        return doctor.spoken(findings, repairs), findings, repairs

    def test_a_healthy_body_says_so(self):
        line, findings, repairs = self.examine(FakeSystem(backup=self.now - 3600))
        self.assertEqual(line, "[joy] I'm well. Everything I can check looks fine.")
        self.assertEqual(repairs, [])
        rows = dict((text.split(":")[0], text) for _, text in doctor.health_rows(FakeSystem(backup=self.now),
                                                                                 self.facts()))
        for area in ("ears and voice", "thinking model", "memory disk", "battery health", "temperature", "clock",
                     "microphone", "last backup", "failed services"):
            self.assertIn(area, rows)
        self.assertEqual(rows["battery health"], "battery health: 81%, 71 cycles")

    def test_restarts_are_told_and_at_most_three_findings_worst_first(self):
        system = FakeSystem(units=unit("being-voice.service", restarts=2), failed=["being-wifi.service"],
                            disk=(120e9, 110e9, 10e9), backup=None, checks={"lists.db": False})
        line, findings, _ = self.examine(system, clock_trusted=False)
        self.assertTrue(line.startswith("[concern] "))
        told = line.removeprefix("[concern] ")
        self.assertEqual(told, "My lists file is damaged. A background service of mine failed: being-wifi. "
                               "My memory disk is getting full.")
        self.assertNotIn("ears", told, "only three, worst first")
        restarted = [f for f in findings if f.area == "organs" and f.severity]
        self.assertEqual(restarted[0].line, "My ears and voice restarted twice since I started; they're fine now.")
        self.assertEqual([f.severity for f in findings], sorted((f.severity for f in findings), reverse=True))

    def test_each_check_reads_its_own_fact(self):
        cases = (
            (FakeSystem(cortex=False), {}, "My thinking model isn't answering."),
            (FakeSystem(), {"voice": {"floor_dbfs": -60, "heard_ago_s": 2 * 86400}}, "for over a day"),
            (FakeSystem(uptime=7200), {"voice": {"floor_dbfs": -60}}, "haven't understood any speech since I started"),
            (FakeSystem(disk=(100e9, 97e9, 3e9)), {}, "nearly full"),
            (FakeSystem(checks={"mind.db": False}), {}, "My memory file is damaged."),
            (FakeSystem(backup=self.now - 30 * 86400 - 60), {}, "My last backup was 30 days ago."),
            (FakeSystem(), {"body": {"health_pct": 55}}, "My battery is worn"),
            (FakeSystem(), {"body": {"battery_low": True}}, "battery is low"),
            (FakeSystem(), {"body": {"cap_honoured": False, "charge_limit": 80}}, "charging past its cap"),
            (FakeSystem(), {"body": {"package_c": 93}}, "I'm very hot: 93 degrees."),
            (FakeSystem(), {"body": {"temp": 84}}, "I'm running hot: 84 degrees."),
            (FakeSystem(), {"clock_trusted": False}, "not sure of the time"),
            (FakeSystem(units=unit("being-face.service", "failed"), restart_ok=False), {}, "My face has stopped."),
            (FakeSystem(units=unit("being-eyes.service", "failed"), restart_ok=False), {}, "My eyes have stopped."),
        )
        for system, changes, words in cases:
            if system.backup is None and "backup" not in words:
                system.backup = time.time()
            line, _, _ = self.examine(system, **changes)
            self.assertIn(words, line, (words, line))

    def test_slow_thinking_against_the_measured_baseline(self):
        doctor.note_turn(prompt_tps=21.9, decode_tps=9.0)
        line, _, _ = self.examine(FakeSystem(backup=self.now))
        self.assertIn("looks fine", line)
        doctor.note_turn(prompt_tps=11.0)
        line, findings, _ = self.examine(FakeSystem(backup=self.now))
        self.assertIn("My thinking is slow: 11 tokens a second, against my usual 22.", line)
        doctor.note_turn(prompt_tps="fast")
        self.assertEqual(doctor._last_speed["prompt"], 11.0, "junk is ignored")

    def test_continuity_warning_appears_in_speech_and_care_without_repairs(self):
        system = FakeSystem(backup=self.now, continuity_warning=True)
        line, _, repairs = self.examine(system)
        self.assertIn("startup check for my saved memory or thinking model needs attention", line)
        self.assertEqual(repairs, [])
        self.assertIn(("warning", "startup continuity: check the archive and model slots"),
                      doctor.health_rows(system, self.facts()))

    def test_no_systemd_and_broken_readings_still_answer(self):
        class Broken(FakeSystem):
            def disk(self, path):
                raise RuntimeError("boom")
        system = Broken(units=False, backup=self.now)
        system.failed = None
        line, findings, repairs = self.examine(system)
        self.assertIn("looks fine", line)
        self.assertIn("disk: could not check (RuntimeError)", [f.row for f in findings])
        self.assertEqual(repairs, [])


class Repairs(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        doctor._repaired.clear()
        doctor._last_speed.clear()

    def facts(self, **changes):
        values = {"state_dir": self.mind, "voice": {"floor_dbfs": -62, "heard_ago_s": 5}, "now": time.time()}
        values.update(changes)
        return doctor.Facts(**values)

    def kinds(self):
        return [entry["kind"] for entry in action_log.entries(self.mind)]

    def test_one_stopped_organ_is_restarted_once_and_logged(self):
        system = FakeSystem(units={**unit("being-voice.service", "failed"), **unit("being-eyes.service", "failed")},
                            backup=time.time())
        findings, repairs = doctor.examine(system, self.facts(), )
        self.assertEqual(system.restarted, ["being-voice.service"], "one organ per check-up")
        self.assertEqual(repairs, ["I restarted my ears and voice."])
        self.assertEqual(self.kinds(), ["repair.organ"])
        self.assertTrue(doctor.spoken(findings, repairs).startswith("[concern] I restarted my ears and voice. "))
        doctor.examine(system, self.facts())
        self.assertEqual(system.restarted, ["being-voice.service", "being-eyes.service"], "the next one, next time")
        doctor.examine(system, self.facts())
        self.assertEqual(len(system.restarted), 2, "never the same organ twice in ten minutes")
        doctor.repair(system, self.facts(), doctor.check(system, self.facts()), now=time.monotonic() + 700)
        self.assertEqual(len(system.restarted), 3)

    def test_stopped_hands_are_named_and_restarted(self):
        self.assertIn("being-hands.service", doctor.ORGANS)
        system = FakeSystem(units=unit("being-hands.service", "failed"), backup=time.time())
        self.assertIn("My hands have stopped.", [f.line for f in doctor.check(system, self.facts())])
        findings, repairs = doctor.examine(system, self.facts())
        self.assertEqual(system.restarted, ["being-hands.service"])
        self.assertEqual(repairs, ["I restarted my hands."])

    def test_never_the_mind_and_never_the_cortex_mid_turn(self):
        system = FakeSystem(units=unit("being-mind.service", "failed"), backup=time.time())
        doctor.examine(system, self.facts())
        self.assertEqual(system.restarted, [])
        system = FakeSystem(units=unit("being-cortex.service", "failed"), cortex=None, backup=time.time())
        doctor.examine(system, self.facts(cortex_busy=True))
        self.assertEqual(system.restarted, [], "the workshop or a turn is using it")
        doctor.examine(system, self.facts(cortex_busy=False))
        self.assertEqual(system.restarted, ["being-cortex.service"])

    def test_a_silent_microphone_reruns_audio_init(self):
        system = FakeSystem(backup=time.time())
        findings, repairs = doctor.examine(system, self.facts(voice={"floor_dbfs": -120}))
        self.assertEqual(system.audio_runs, 1)
        self.assertIn("I reset my microphone", repairs[0])
        self.assertEqual(self.kinds(), ["repair.audio"])
        doctor.examine(system, self.facts(voice={"floor_dbfs": -120}))
        self.assertEqual(system.audio_runs, 1, "rate-limited")
        doctor._repaired.clear()
        doctor.examine(system, self.facts(voice={"floor_dbfs": -89}))
        self.assertEqual(system.audio_runs, 0 + 1, "above -90 dBFS is not silence")
        failing = FakeSystem(backup=time.time(), audio_ok=False)
        _, repairs = doctor.examine(failing, self.facts(voice={"floor_dbfs": -100}))
        self.assertEqual(repairs, [], "a repair that failed is not claimed")


class RealHardware(unittest.TestCase):
    """What the laptop's units and microphone really do."""

    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        doctor._repaired.clear()
        doctor._last_speed.clear()

    def facts(self, **changes):
        values = {"state_dir": self.mind, "voice": {"floor_dbfs": -62, "heard_ago_s": 5}, "now": time.time()}
        values.update(changes)
        return doctor.Facts(**values)

    def test_an_organ_that_is_off_on_purpose_is_not_stopped(self):
        # The eyes' unit has ConditionPathExists=/mind/camera.enabled: with the
        # camera off it reads inactive, condition "no". Never restarted.
        off = {"being-eyes.service": {"Id": "being-eyes.service", "LoadState": "loaded", "ActiveState": "inactive",
                                      "SubState": "dead", "Result": "success", "ConditionResult": "no",
                                      "NRestarts": "0"},
               # Stopped cleanly (the night shift freeing memory): off, not broken.
               "being-cortex.service": {"Id": "being-cortex.service", "LoadState": "loaded",
                                        "ActiveState": "inactive", "Result": "success", "ConditionResult": "yes"},
               # Not in this image at all.
               "being-bodycare.service": {"Id": "being-bodycare.service", "LoadState": "not-found",
                                          "ActiveState": "inactive", "Result": "success"}}
        system = FakeSystem(units=off, backup=time.time(), cortex=None)
        findings, repairs = doctor.examine(system, self.facts())
        self.assertEqual((system.restarted, repairs), ([], []))
        rows = [f.row for f in findings]
        self.assertIn("eyes: off", rows)
        self.assertIn("thinking model: off", rows)
        self.assertFalse(any(row.startswith("body care") for row in rows))
        self.assertIn("looks fine", doctor.spoken(findings, repairs))
        # Ended badly while inactive (an exit code): stopped, and restarted.
        crashed = {"being-face.service": {"Id": "being-face.service", "LoadState": "loaded", "ActiveState": "inactive",
                                          "Result": "exit-code", "ConditionResult": "yes"}}
        system = FakeSystem(units=crashed, backup=time.time())
        findings, repairs = doctor.examine(system, self.facts())
        self.assertEqual((system.restarted, repairs), (["being-face.service"], ["I restarted my face."]))
        self.assertNotIn("has stopped", doctor.spoken(findings, repairs), "a repaired organ is not told as broken")

    def test_a_muted_or_closed_microphone_is_never_reset(self):
        system = FakeSystem(backup=time.time())
        for body in ({"muted": True}, {"lid_closed": True}):
            findings, repairs = doctor.examine(system, self.facts(voice={"floor_dbfs": -120}, body=body))
            self.assertEqual((system.audio_runs, repairs), (0, []), body)
            self.assertNotIn("silence", doctor.spoken(findings, repairs))

    def test_a_mixer_that_stays_silent_is_reset_at_most_three_times_a_start(self):
        system = FakeSystem(backup=time.time())
        for attempt in range(6):
            doctor.repair(system, self.facts(voice={"floor_dbfs": -120}),
                          doctor.check(system, self.facts(voice={"floor_dbfs": -120})), now=1000.0 + 700 * attempt)
        self.assertEqual(system.audio_runs, doctor.AUDIO_RESETS_PER_START)

    def test_a_short_warm_prompt_is_not_judged_against_the_baseline(self):
        doctor.note_turn(prompt_tps=6.0, decode_tps=8.0, prompt_n=4)
        self.assertNotIn("prompt", doctor._last_speed, "four new tokens are mostly fixed cost")
        self.assertEqual(doctor._last_speed["decode"], 8.0)
        doctor.note_turn(prompt_tps=12.0, prompt_n=120)
        self.assertEqual(doctor._last_speed["prompt"], 12.0)

    def test_the_automatic_pass_reads_no_databases(self):
        class NoDisk(FakeSystem):
            def quick_check(self, path):
                raise AssertionError("self-care must not read every database every ten minutes")

            def cortex_ok(self):
                raise AssertionError("nor ask the cortex")
        system = NoDisk(units=unit("being-voice.service", "failed"), backup=time.time())
        ctx = SimpleNamespace(state_dir=self.mind, body={}, voice={"floor_dbfs": -60}, clock_trusted=True)
        self.assertEqual(asyncio.run(doctor.self_care(ctx, system=system)), ["I restarted my ears and voice."])
        self.assertEqual(action_log.entries(self.mind)[0]["kind"], "repair.organ")

    def test_repairs_in_session_only_mode_leave_no_durable_log_line(self):
        from mind.retention import MemoryPolicy, save_policy

        save_policy(self.mind, MemoryPolicy("session-only"))
        system = FakeSystem(units=unit("being-voice.service", "failed"), backup=time.time())
        ctx = SimpleNamespace(state_dir=self.mind, body={}, voice={"floor_dbfs": -60}, clock_trusted=True)
        self.assertEqual(asyncio.run(doctor.self_care(ctx, system=system)), ["I restarted my ears and voice."])
        self.assertEqual(action_log.entries(self.mind), [])
        self.assertFalse(action_log.log_path(self.mind).exists())

    def test_an_unopenable_database_is_unknown_not_damaged(self):
        system = doctor.System(systemctl="definitely-not-systemctl")
        path = self.mind / "mind.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE t (x)")
        with patch("mind.doctor.sqlite3.connect", side_effect=sqlite3.OperationalError("unable to open database")):
            self.assertIsNone(system.quick_check(path))

    def test_a_loading_cortex_is_not_reported_as_broken(self):
        statuses = {"code": 503}

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(statuses["code"])
                self.end_headers()

            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Health)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        system = doctor.System(systemctl="definitely-not-systemctl", cortex_port=server.server_address[1])
        self.assertIsNone(system.cortex_ok(), "503: still loading its model")
        statuses["code"] = 200
        self.assertTrue(system.cortex_ok())
        statuses["code"] = 500
        self.assertFalse(system.cortex_ok())


class Handler(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))
        doctor._repaired.clear()
        doctor._last_speed.clear()

    def ctx(self, guest=False):
        return SimpleNamespace(state_dir=self.mind, guest=guest, body={"temp": 50}, voice={"floor_dbfs": -60,
                                                                                             "heard_ago_s": 3},
                               clock_trusted=True, send=None)

    def test_phrases(self):
        for text in ("Are you okay?", "Run a check-up", "What's wrong?", "hey pip, are you alright",
                     "do a health check", "is anything wrong with you", "check yourself"):
            self.assertTrue(doctor.may_match(text), text)
            reply = asyncio.run(doctor.handle(text, self.ctx(), system=FakeSystem(backup=time.time())))
            self.assertIn("looks fine", reply.line, text)
        for text in ("what's wrong with my code", "are you okay with that", "how are you", "check the timer",
                     "what's the weather"):
            self.assertFalse(doctor.may_match(text), text)
            self.assertIsNone(asyncio.run(doctor.handle(text, self.ctx(), system=FakeSystem())), text)

    def test_by_voice_each_database_check_is_time_bounded_and_f2_reads_it_all(self):
        system = FakeSystem(backup=time.time())
        asyncio.run(doctor.handle("are you okay", self.ctx(), system=system))
        self.assertEqual(system.budgets, [doctor.VOICE_CHECK_S] * len(doctor.DATABASES))
        full = FakeSystem(backup=time.time())
        doctor.health_rows(full, doctor.Facts(state_dir=self.mind))
        self.assertEqual(full.budgets, [None] * len(doctor.DATABASES), "the Health page checks every page")

    def test_a_check_that_runs_out_of_time_is_unfinished_not_damaged(self):
        system = doctor.System(systemctl="definitely-not-systemctl")
        big = self.mind / "mind.db"
        with closing(sqlite3.connect(big)) as db, db:
            db.execute("CREATE TABLE t (x TEXT)")
            db.executemany("INSERT INTO t VALUES (?)", (("x" * 200,) for _ in range(20000)))
            db.execute("CREATE INDEX t_x ON t (x)")
        self.assertEqual(system.quick_check(big, budget_s=0), doctor.UNFINISHED)
        self.assertTrue(system.quick_check(big, budget_s=60))
        self.assertTrue(system.quick_check(big))
        slow = FakeSystem(checks={"mind.db": doctor.UNFINISHED, "lists.db": True})
        rows = [finding.row for finding in doctor.check_databases(slow, doctor.Facts(state_dir=self.mind,
                                                                                     check_budget_s=1.0))]
        self.assertIn("mind.db: not checked in full (the Health page does)", rows)
        self.assertTrue(all(finding.severity == doctor.OK
                            for finding in doctor.check_databases(slow, doctor.Facts(state_dir=self.mind))))

    def test_guests_get_a_polite_answer_and_no_repairs(self):
        system = FakeSystem(units=unit("being-voice.service", "failed"))
        reply = asyncio.run(doctor.handle("are you okay", self.ctx(guest=True), system=system))
        self.assertIn("owner", reply.line)
        self.assertEqual(system.restarted, [])

    def test_quick_check_on_real_files(self):
        system = doctor.System(systemctl="definitely-not-systemctl")
        good = self.mind / "lists.db"
        with sqlite3.connect(good) as db:
            db.execute("CREATE TABLE t (x)")
        self.assertTrue(system.quick_check(good))
        bad = self.mind / "mind.db"
        bad.write_bytes(b"SQLite format 3\0" + b"\xff" * 4096)
        self.assertFalse(system.quick_check(bad))
        self.assertIsNone(system.quick_check(self.mind / "reminders.db"))
        self.assertIsNone(system.units(["being-voice.service"]), "no systemd: unknown, not broken")


if __name__ == "__main__":
    unittest.main()

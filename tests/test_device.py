"""Pip's own controls by voice: mute, brightness, power, version, update, backup."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from mind import action_log, backup, device


class Ctx(SimpleNamespace):
    pass


def make_ctx(state_dir, *, guest=False, body=None, fail=False, answer=None):
    """`answer`: what the mind's send returns for being.brightness (the
    brainstem's body.brightness fields), or None as when none came in time."""
    sent = []

    async def send(topic, **fields):
        if fail:
            raise ConnectionError("bus gone")
        sent.append((topic, fields))
        return answer if topic == "being.brightness" else None
    return Ctx(state_dir=Path(state_dir), guest=guest, body=body or {}, voice={}, send=send, sent=sent)


def run(coro):
    return asyncio.run(coro)


class Controls(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def ask(self, text, **options):
        ctx = make_ctx(self.mind, **options)
        return run(device.handle(text, ctx)), ctx.sent

    def test_stop_listening_presses_mute_on_and_says_how_to_undo_it(self):
        for text in ("Stop listening", "Hey Pip, stop listening to me.", "mute yourself", "don't listen for a bit"):
            reply, sent = self.ask(text)
            self.assertEqual(reply.line, "[neutral] Okay, I'm not listening. Press M to let me listen again.", text)
            self.assertEqual(sent, [("key.mute", {"on": True})], "never a toggle")
            self.assertEqual(reply.logged, "mute")
        reply, _ = self.ask("stop listening", fail=True)
        self.assertIn("Press M", reply.line)
        self.assertIn("couldn't", reply.line, "a lost bus is said, not hidden")

    def test_brightness_steps_and_levels(self):
        for text, fields, line in (
            ("Brighter", {"step": 1}, "[neutral] Brighter."), ("be brighter please", {"step": 1}, "[neutral] Brighter."),
            ("turn up your brightness", {"step": 1}, "[neutral] Brighter."), ("Dimmer", {"step": -1}, "[neutral] Dimmer."),
            ("make your screen darker", {"step": -1}, "[neutral] Dimmer."),
            ("Set your brightness to 50 percent", {"level": 50}, "[neutral] Brightness 50 percent."),
            ("set brightness to seventy five", {"level": 75}, "[neutral] Brightness 75 percent."),
            ("full brightness", {"level": 100}, "[neutral] Brightness 100 percent."),
            ("set your brightness to 0", {"level": 0}, "[neutral] Brightness 5 percent."),
        ):
            reply, sent = self.ask(text)
            self.assertEqual((reply.line, sent), (line, [("being.brightness", fields)]), text)
        reply, sent = self.ask("set your brightness to 300 percent")
        self.assertEqual(sent, [])
        self.assertIn("5 to 100", reply.line)
        for text in ("your brightness is fine", "brightness is too much for me"):
            self.assertIsNone(self.ask(text)[0], "not a request")
        reply, sent = self.ask("brighter", body={"brightness_reason": "no_control"})
        self.assertEqual((reply.line, sent), ("[neutral] I can't change my brightness on this machine.", []))

    def test_the_reply_follows_the_brainstems_answer_at_the_limits(self):
        top = {"ok": True, "level": 100, "at_max": True, "at_min": False, "limited": True}
        bottom = {"ok": True, "level": 5, "at_max": False, "at_min": True, "limited": True}
        middle = {"ok": True, "level": 65, "at_max": False, "at_min": False, "limited": False}
        for text, answer, line in (
            ("Brighter", top, "[neutral] That's as bright as I go."),
            ("turn up your brightness", top, "[neutral] That's as bright as I go."),
            ("Dimmer", bottom, "[neutral] That's as dim as I go."),
            ("Brighter", middle, "[neutral] Brighter."),
            ("Dimmer", middle, "[neutral] Dimmer."),
            ("set your brightness to 0", bottom, "[neutral] Brightness 5 percent. That's as dim as I go."),
            ("Set your brightness to 50 percent", dict(middle, level=50), "[neutral] Brightness 50 percent."),
            ("full brightness", dict(top, limited=False), "[neutral] Brightness 100 percent."),
        ):
            reply, sent = self.ask(text, answer=answer)
            self.assertEqual(reply.line, line, (text, answer))
            self.assertEqual(reply.logged, "brightness")
            self.assertEqual(len(sent), 1)

    def test_a_change_that_failed_is_never_said_as_done(self):
        reply, _ = self.ask("brighter", answer={"ok": False, "reason": "write_failed", "level": 40})
        self.assertEqual(reply.line, "[concern] My screen didn't change just now.")
        self.assertIsNone(reply.logged, "nothing changed, so nothing is logged")
        reply, _ = self.ask("brighter", answer={"ok": False, "reason": "no_control"})
        self.assertEqual(reply.line, "[neutral] I can't change my brightness on this machine.")

    def test_switching_off_and_restarting_always_ask_first(self):
        reply, sent = self.ask("Shut down")
        self.assertEqual(reply.line, "[neutral] Shall I switch off? Say yes.")
        self.assertIsNone(reply.action, "nothing happens before the yes")
        self.assertEqual(sent, [])
        yes = run(reply.confirm.on_yes())
        self.assertEqual((yes.action, yes.logged), ("poweroff", "shutdown"))
        self.assertIn("Goodbye", yes.line)
        self.assertEqual(reply.confirm.on_no.line, "[neutral] Okay, I've left it.")
        self.assertIs(reply.confirm.strict, True, "only a plain yes switches it off")
        for text in ("switch off", "Switch yourself off", "turn yourself off", "power down"):
            self.assertEqual(self.ask(text)[0].line, "[neutral] Shall I switch off? Say yes.", text)
        reply, _ = self.ask("Restart yourself")
        self.assertEqual(reply.line, "[neutral] Shall I restart? Say yes.")
        self.assertEqual(run(reply.confirm.on_yes()).action, "reboot")
        self.assertIs(reply.confirm.strict, True, "only a plain yes restarts it")
        reply, _ = self.ask("shut down", guest=True)
        self.assertIsNone(reply.confirm)
        self.assertIn("Only my owner", reply.line)

    def test_near_misses_pass_on(self):
        for text in ("turn off the terminal", "shut the terminal", "stop", "restart the game", "switch off the clock",
                     "stop the timer", "close the terminal", "what's the brightest star", "listen to this",
                     "update my shopping list", "back to the start", "turn off the lights"):
            reply, sent = self.ask(text)
            self.assertIsNone(reply, text)
            self.assertFalse(device.may_match(text), text)
            self.assertEqual(sent, [])

    def test_version_comes_from_the_build_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "build-id"
            path.write_text("3f2a9c1b" + "0" * 56 + "\n")
            self.assertEqual(device.version_line(path),
                             "[neutral] I'm PipOS, build 3f2a9c1b. The full number is on the Update page under F2.")
            path.write_text("<script>\n")
            self.assertIn("can't find my build number", device.version_line(path))
        self.assertIn("can't find", device.version_line(Path("/nonexistent/build-id")))
        self.assertEqual(device.classify("What version are you?"), ("version", None))


class Updates(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mind = self.work / "mind"
        self.mind.mkdir()
        self.cmdline = self.work / "cmdline"
        self.good = self.work / "slot-good"
        self.uptime = self.work / "uptime"
        self.now = 1_800_000_000

    def line(self, cmdline, good=None, up=600.0):
        self.cmdline.write_text(cmdline)
        if good is None:
            self.good.unlink(missing_ok=True)
        else:
            self.good.write_text(good + "\n")
        self.uptime.write_text(f"{up} 1\n")
        return device.update_line(self.mind, cmdline=self.cmdline, slot_good=self.good, uptime_path=self.uptime,
                                  now=self.now)

    def test_did_your_update_work_answers_from_boot_health_and_the_log(self):
        self.assertIn("Try stick", self.line("quiet being.try=1"))
        self.assertIn("haven't installed an update lately", self.line("rauc.slot=B quiet", "B"))
        action_log.record(self.mind, "update.install", now=self.now - 86400)
        self.assertEqual(self.line("rauc.slot=B quiet", "B"),
                         "[joy] Yes. I started on the new system and passed my health check. The update went in yesterday.")
        self.assertIn("still checking", self.line("rauc.slot=B", None, up=40))
        self.assertIn("Not yet", self.line("rauc.slot=B", None, up=900))
        self.assertIn("Not yet", self.line("rauc.slot=B", "A", up=900), "the good slot is the other one")
        self.assertIn("didn't come through", self.line("quiet", None, up=900))
        self.assertEqual(device.classify("Did your update work?"), ("update", None))


class Backups(unittest.TestCase):
    def setUp(self):
        self.mind = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_when_did_you_last_back_up(self):
        reply = run(device.handle("When did you last back up?", make_ctx(self.mind)))
        self.assertIn("haven't been backed up yet", reply.line)
        backup.record_backup(self.mind, "night", now=time.time() - 3 * 3600)
        reply = run(device.handle("when were you last backed up", make_ctx(self.mind)))
        self.assertEqual(reply.line, "[neutral] My last backup was 3 hours ago.")
        backup.record_backup(self.mind, "voice", now=time.time() - 20 * 86400)
        self.assertEqual(device.last_backup_line(self.mind), "[concern] My last backup was 20 days ago.")
        reply = run(device.handle("are you backed up", make_ctx(self.mind, guest=True)))
        self.assertIn("my owner", reply.line)

    def test_back_yourself_up_says_what_is_missing_then_asks(self):
        for reason, words in (("no_stick", "labelled being-backup"), ("no_key", "F2 menu"), ("no_tool", "no backup tool")):
            def missing(state_dir, reason=reason):
                raise backup.Unavailable(reason)
            reply = run(device.back_up(make_ctx(self.mind), readiness=missing))
            self.assertIn(words, reply.line)
            self.assertIsNone(reply.confirm)
        ran = []
        current = device._VoiceBackup()
        ready = lambda state_dir: ("/dev/sdb1", "age1x")
        reply = run(device.back_up(make_ctx(self.mind), readiness=ready, run=lambda: ran.append("backup"),
                                   current=current))
        self.assertEqual(reply.line, "[neutral] Shall I back myself up to your stick now? Say yes.")
        self.assertEqual(ran, [], "nothing is written before the yes")
        done = run(reply.confirm.on_yes())
        self.assertEqual((done.line, ran), ("[joy] Done. I've backed myself up to your stick.", ["backup"]))
        self.assertIsNone(done.logged, "the backup logs itself when it really ends")
        self.assertEqual([entry["kind"] for entry in action_log.entries(self.mind)], ["backup"])

        def broken():
            raise backup.Unavailable("failed", "disk full")
        reply = run(device.back_up(make_ctx(self.mind), readiness=ready, run=broken, current=current))
        failed = run(reply.confirm.on_yes())
        self.assertIn("didn't work", failed.line)
        self.assertEqual(action_log.entries(self.mind)[-1]["kind"], "backup.failed")
        self.assertIn("something went wrong writing to the stick", device.last_backup_line(self.mind, current=current))

    def test_a_voice_backup_runs_low_tells_prune_the_clock_and_logs_nothing_in_session_only(self):
        from unittest.mock import patch

        from mind.retention import MemoryPolicy, save_policy

        calls = []
        ctx = make_ctx(self.mind)
        ctx.clock_trusted = False
        current = device._VoiceBackup()
        with patch("mind.backup.stick_backup", lambda state_dir, how, **options: calls.append((how, options))), \
                patch("mind.backup.lower_priority") as lowered:
            reply = run(device.back_up(ctx, readiness=lambda state_dir: ("/dev/sdb1", "age1x"), current=current))
            done = run(reply.confirm.on_yes())
        self.assertEqual(done.line, "[joy] Done. I've backed myself up to your stick.")
        self.assertEqual(calls, [("voice", {"clock_trusted": False})], "prune is told the clock is in doubt")
        lowered.assert_called_once_with()
        # "Nothing after a restart": the backup still happens, but no durable log line says so.
        save_policy(self.mind, MemoryPolicy("session-only"))
        reply = run(device.back_up(make_ctx(self.mind), readiness=lambda state_dir: ("/dev/sdb1", "age1x"),
                                   run=lambda: None, current=current))
        run(reply.confirm.on_yes())
        self.assertEqual([entry["kind"] for entry in action_log.entries(self.mind)], ["backup"])

    def test_a_slow_backup_answers_at_once_and_carries_on(self):
        release = threading.Event()
        current = device._VoiceBackup()
        reply = run(device.back_up(make_ctx(self.mind), readiness=lambda state_dir: ("/dev/sdb1", "age1x"),
                                   run=lambda: (release.wait(5), backup.record_backup(self.mind, "voice")),
                                   current=current, answer_within=0.2))
        began = time.monotonic()
        later = run(reply.confirm.on_yes())
        self.assertLess(time.monotonic() - began, 2.0, "the turn is not held by the stick")
        self.assertIn("still writing", later.line)
        self.assertEqual(device.last_backup_line(self.mind, current=current),
                         "[neutral] I'm still backing myself up. Leave the stick in.")
        again = run(device.back_up(make_ctx(self.mind), readiness=lambda state_dir: ("/dev/sdb1", "age1x"),
                                   current=current))
        self.assertEqual(again.line, "[neutral] I'm already backing myself up.")
        release.set()
        current.thread.join(5)
        self.assertEqual(device.last_backup_line(self.mind, current=current),
                         "[neutral] My last backup was less than an hour ago.")
        self.assertEqual([entry["kind"] for entry in action_log.entries(self.mind)], ["backup"])

    def test_phrases(self):
        for text in ("Back yourself up", "back up", "make a backup", "Hey Pip, do a backup please"):
            self.assertEqual(device.classify(text), ("back_up", None), text)
        for text in ("When did you last back up?", "have you backed up lately", "when was your last backup",
                     "did the backup work", "is the backup done"):
            self.assertEqual(device.classify(text), ("last_backup", None), text)


if __name__ == "__main__":
    unittest.main()

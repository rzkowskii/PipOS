import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mind import setup
from mind.initiative import InitiativePolicy, QuietHours, load_policy as load_initiative_policy
from mind.network_policy import NetworkPolicy, save_policy
from mind.profile import PROFILE_FILE, Personality, Profile


class SetupTests(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.mind_dir = Path(work) / "mind"
        self.args = ["--config", str(Path(__file__).resolve().parents[1] / "config"),
                     "--mind-dir", str(self.mind_dir)]

    def enter_choices(self, choices, *args):
        with (patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=choices),
              patch.object(setup, "available_voices", return_value={"slt", "awb"}), redirect_stdout(io.StringIO())):
            return setup.main(self.args + list(args))

    def test_cancel_and_dry_run_leave_no_profile(self):
        self.assertEqual(0, self.enter_choices(["1", "Clover", "q"]))
        self.assertEqual(0, self.enter_choices(["1", "Clover", "k", "yes"], "--dry-run"))
        self.assertFalse((self.mind_dir / PROFILE_FILE).exists())

    def test_keep_uses_the_reviewed_roll_and_preserves_wake_phrase_on_rename(self):
        first = Personality(0, 1, 2, 0, 1)
        second = Personality(1, 0, 1, 2, 0)
        with patch.object(setup, "random_personality", side_effect=[first, second]):
            self.assertEqual(0, self.enter_choices(["1", "Clover", "4", "2", "6", "5", "r", "k", "y"]))
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, setup.main(self.args + ["--show"]))
        profile = json.loads(output.getvalue())
        self.assertEqual(("Clover", "Hey Pip", "awb"), (profile["name"], profile["wake_word"], profile["voice"]))
        self.assertEqual({"warmth": 1, "curiosity": 0, "humour": 1, "expressiveness": 2, "detail": 0},
                         profile["personality"])

    def test_saved_try_profile_reloads_active_organs_and_dry_run_does_not(self):
        with (patch.object(setup, "is_try_boot", return_value=True),
              patch.object(setup, "load_profile", return_value=Profile()),
              patch.object(setup, "save_profile") as save,
              patch.object(setup, "restart_try_organs", new_callable=AsyncMock) as restart):
            self.assertEqual(0, self.enter_choices(["1", "Clover", "k", "yes"], "--mind-dir", "/mind", "--dry-run"))
            save.assert_not_called()
            restart.assert_not_awaited()
            self.assertEqual(0, self.enter_choices(["1", "Clover", "k", "yes"], "--mind-dir", "/mind"))
        self.assertEqual(save.call_args.args[0], Path("/mind"))
        self.assertEqual(save.call_args.args[1].name, "Clover")
        restart.assert_awaited_once_with()

    def test_network_setup_applies_independently_consenting_to_web_and_time(self):
        self.mind_dir.mkdir()
        save_policy(self.mind_dir, NetworkPolicy("internet", True))
        with (patch.object(setup, "APPLIANCE_MIND", self.mind_dir.resolve()), patch.object(setup, "is_try_boot", return_value=False),
              patch.object(setup.subprocess, "run") as run):
            run.return_value.returncode = 0
            self.assertEqual(0, self.enter_choices(["3", "y", "clock.test", "fd00:0000::7", "yes"], "--network"))
        run.assert_called_once_with([setup.NETWORK_COMMAND, "set", "internet", "--web-lookups",
                                     "--time-server", "fd00::7"], check=False)
        self.assertFalse((self.mind_dir / PROFILE_FILE).exists())

    def test_local_setup_menu_can_select_paired_lan_without_web_or_clock_flags(self):
        with (patch.object(setup, "APPLIANCE_MIND", self.mind_dir.resolve()), patch.object(setup, "is_try_boot", return_value=False),
              patch.object(setup.subprocess, "run") as run):
            run.return_value.returncode = 0
            self.assertEqual(0, self.enter_choices(["8", "2", "yes", "q"]))
        run.assert_called_once_with([setup.NETWORK_COMMAND, "set", "paired-lan"], check=False)

    def test_network_setup_requires_confirmation_and_is_unavailable_in_try(self):
        with (patch.object(setup, "APPLIANCE_MIND", self.mind_dir.resolve()), patch.object(setup, "is_try_boot") as try_boot,
              patch.object(setup.subprocess, "run") as run):
            try_boot.return_value = False
            self.assertEqual(0, self.enter_choices(["1", "n"], "--network"))
            self.assertEqual(0, self.enter_choices(["8", "q"], "--dry-run"))
            try_boot.return_value = True
            with redirect_stderr(io.StringIO()):
                self.assertEqual(1, self.enter_choices([], "--network"))
        run.assert_not_called()

    def test_quiet_hours_submenu_saves_separately_from_personalization(self):
        self.assertEqual(0, self.enter_choices(["9", "1", "2", "3", "22:00", "07:00", "k", "q"]))
        self.assertEqual(load_initiative_policy(self.mind_dir, strict=True),
                         InitiativePolicy(False, True, QuietHours("22:00", "07:00")))
        self.assertFalse((self.mind_dir / PROFILE_FILE).exists())

    def test_quiet_hours_direct_save_clear_discard_and_preview(self):
        self.assertEqual(0, self.enter_choices(["3", "22:00", "07:00", "k"], "--initiative"))
        saved = InitiativePolicy(True, False, QuietHours("22:00", "07:00"))
        self.assertEqual(load_initiative_policy(self.mind_dir, strict=True), saved)
        self.assertEqual(0, self.enter_choices(["9", "4", "k", "q"], "--dry-run"))
        self.assertEqual(0, self.enter_choices(["1", "4", "q"], "--initiative"))
        self.assertEqual(0, self.enter_choices(["3", "24:30", "07:00", "3", "22:00", "22:00", "k"],
                                               "--initiative"))
        self.assertEqual(load_initiative_policy(self.mind_dir, strict=True), saved)
        self.assertEqual(0, self.enter_choices(["4", "k"], "--initiative"))
        self.assertEqual(load_initiative_policy(self.mind_dir, strict=True), InitiativePolicy())



class ConsoleSetupTests(SetupTests):
    """Every setup flow also works drawn for being-console."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"BEING_CONSOLE_UI": "1", "COLUMNS": "68", "LINES": "32"}))
        setup.ui.discard_notes()
        self.addCleanup(setup.ui.discard_notes)

    def test_screens_show_the_profile_card_and_keep_prompts(self):
        with (patch("sys.stdin.isatty", return_value=True),
              patch("builtins.input", side_effect=["x", "1", "Clover", "6", "0", "k", "yes"]) as keyboard,
              patch.object(setup, "available_voices", return_value={"slt"}),
              redirect_stdout(io.StringIO()) as output):
            self.assertEqual(0, setup.main(self.args))
        text = setup.ui.plain(output.getvalue())
        prompts = [setup.ui.plain(call.args[0]) for call in keyboard.call_args_list]
        self.assertTrue(prompts[2].endswith("Name [Pip]: "), prompts[2])
        self.assertTrue(prompts[-1].endswith("Save these choices? [y/N] "))
        self.assertIn("Make it yours", text)
        self.assertIn("Clover calls you", text)
        self.assertIn("Warmth ●●●", text)
        self.assertIn("Choose a number, P, K, or Q.", text)
        self.assertIn("Warm and curious", text)
        self.assertIn("current", text)
        self.assertEqual(json_name(self), "Clover")


def json_name(case):
    with redirect_stdout(io.StringIO()) as output:
        setup.main(case.args + ["--show"])
    return json.loads(output.getvalue())["name"]

class PreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        self.socket = self.directory / "bus.sock"
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.muted = True
        self.closed = False
        self.enterContext(patch.dict(os.environ, {"BEING_BUS": str(self.socket)}))
        self.enterContext(patch.object(setup, "FW", self.directory))
        self.enterContext(patch.object(setup, "child", self.audio_player))
        await self.enterAsyncContext(await asyncio.start_unix_server(self.send_readings, path=self.socket))

    async def send_readings(self, reader, writer):
        async def send(event):
            writer.write((json.dumps(event) + "\n").encode())
            await writer.drain()

        try:
            await reader.readline()
            await send({"t": "body.lid", "closed": self.closed})
            await send({"t": "state.changed", "state": "idle", "muted": self.muted})
            if self.muted and not self.closed:
                await self.started.wait()
                await send({"t": "body.lid", "closed": True})
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    @asynccontextmanager
    async def audio_player(self, *_command, **_options):
        class Player:
            async def wait(self):
                await asyncio.Future()

        self.started.set()
        try:
            yield Player()
        finally:
            self.stopped.set()

    async def test_does_not_play_while_microphone_is_on(self):
        self.muted = False
        self.assertFalse(await setup.play_preview(self.directory / "sample.wav"))
        self.assertFalse(self.started.is_set())

    async def test_lid_close_stops_playback(self):
        self.assertFalse(await asyncio.wait_for(setup.play_preview(self.directory / "sample.wav"), 2))
        self.assertTrue(self.started.is_set())
        self.assertTrue(self.stopped.is_set())


class SystemEntries(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.mind_dir = Path(work) / "mind"
        self.mind_dir.mkdir()
        self.args = ["--config", str(Path(__file__).resolve().parents[1] / "config"), "--mind-dir", str(self.mind_dir)]

    def run_setup(self, keys, *args, try_boot=False):
        with (patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=keys),
              patch.object(setup, "available_voices", return_value={"slt"}),
              patch.object(setup, "APPLIANCE_MIND", self.mind_dir.resolve()),
              patch.object(setup, "is_try_boot", return_value=try_boot),
              redirect_stdout(io.StringIO()) as output):
            return setup.main(self.args + list(args)), output.getvalue()

    def test_installed_setup_offers_care_sound_zone_backup_and_update(self):
        with (patch.object(setup.care, "care_menu") as care_menu, patch.object(setup.care, "update_menu") as update,
              patch.object(setup.care, "backup_menu") as backup):
            code, text = self.run_setup(["c", "u", "b", "q"])
        self.assertEqual(code, 0)
        for label in ("C  Senses and memory", "A  Volume and microphone", "T  Time zone",
                      "B  Back up to a USB stick", "U  Update from a USB stick"):
            self.assertIn(label, text)
        care_menu.assert_called_once_with(self.mind_dir, temporary=False)
        update.assert_called_once_with(try_boot=False)
        backup.assert_called_once_with(self.mind_dir, temporary=False)

    def test_installed_setup_offers_typing_and_the_voice_hint_names_its_key(self):
        from mind import type_chat

        with patch.object(type_chat, "main", return_value=0) as chat:
            code, text = self.run_setup(["y", "q"])
        self.assertEqual(code, 0)
        self.assertIn(f"{setup.TYPE_CHAT_KEY}  Talk to ", text)
        chat.assert_called_once_with(mind_dir=self.mind_dir)
        self.assertEqual(type_chat.typing_hint(try_boot=False),
                         "You can also type to me: press Mission Control, or fn and F2, "
                         f"then {setup.TYPE_CHAT_KEY}.")
        self.assertEqual(type_chat.typing_hint(try_boot=True),
                         "You can also type to me: press Mission Control, or fn and F2, then 6.")

    def test_try_setup_has_care_but_no_update_or_backup_entries(self):
        with patch.object(setup.care, "care_menu") as care_menu:
            code, text = self.run_setup(["c", "q"], try_boot=True)
        self.assertEqual(code, 0)
        self.assertIn("C  Senses and memory", text)
        self.assertNotIn("Update from a USB stick", text)
        care_menu.assert_called_once_with(self.mind_dir, temporary=True)

    def test_off_appliance_setup_shows_only_the_profile(self):
        with (patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["q"]),
              patch.object(setup, "available_voices", return_value={"slt"}), redirect_stdout(io.StringIO()) as output):
            setup.main(self.args)
        self.assertNotIn("Senses and memory", output.getvalue())

    def test_try_restart_names_only_installed_units(self):
        from types import SimpleNamespace

        calls = []

        @asynccontextmanager
        async def child(*command, **_):
            calls.append(command)
            yield SimpleNamespace(wait=AsyncMock(return_value=0))

        class FakeBus:
            def __init__(self, *_, **__):
                self.events = iter([{"t": "state.changed"}, {"t": "organ.up", "name": "mind"},
                                    {"t": "organ.up", "name": "voice"}])

            async def start(self):
                pass

            async def stop(self):
                pass

            async def recv(self):
                return next(self.events)

        units = self.mind_dir / "units"
        units.mkdir()
        for unit in ("being-mind.service", "being-voice.service"):
            (units / unit).write_text("")
        with (patch.object(setup, "Bus", FakeBus), patch.object(setup, "child", child),
              patch("mind.network_enforce.UNIT_DIRS", (units,))):
            asyncio.run(setup.restart_try_organs())
        self.assertEqual(calls, [("systemctl", "try-restart", "being-mind.service", "being-voice.service")])


class VoicePageTests(unittest.TestCase):
    """The natural (Piper) or original (Flite) voice page."""

    def setUp(self):
        from mind import voice_engines
        from tests.test_voice_engines import FakePiper

        self.voice_engines = voice_engines
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.mind_dir = Path(work) / "mind"
        self.mind_dir.mkdir()
        self.args = ["--config", str(Path(__file__).resolve().parents[1] / "config"), "--mind-dir", str(self.mind_dir)]
        self.piper = FakePiper()
        self.enterContext(patch.object(voice_engines, "_piper", return_value=self.piper))
        self.enterContext(patch.object(voice_engines, "build_id", return_value="b1"))

    def run_setup(self, keys, *args, appliance=False):
        with (patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=keys),
              patch.object(setup, "available_voices", return_value={"slt"}),
              patch.object(setup, "APPLIANCE_MIND", self.mind_dir.resolve() if appliance else Path("/mind")),
              patch.object(setup, "is_try_boot", return_value=False),
              redirect_stdout(io.StringIO()) as output):
            return setup.main(self.args + list(args)), output.getvalue()

    def test_page_lists_both_voices_and_saves_the_natural_one(self):
        code, text = self.run_setup(["n", "2", "q", "q"])
        self.assertEqual(code, 0)
        for label in ("N  Natural or original voice", "1  Automatic", "2  Natural (Alba)", "3  Original",
                      "Not measured on this laptop yet", "Play the natural voice"):
            self.assertIn(label, text)
        self.assertEqual(self.voice_engines.load_preference(self.mind_dir), "piper")

    def test_measured_speed_and_automatic_choice_are_shown(self):
        self.voice_engines.record(self.mind_dir, "tts", self.voice_engines.tts_key("b1", self.piper),
                                  {"rtf": 0.28, "runs": [0.28, 0.27, 0.3]})
        code, text = self.run_setup(["q"], "--voice")
        self.assertEqual(code, 0)
        self.assertIn("0.28 of real time (fast enough)", text)
        self.assertIn("Automatic, by measured speed", text)
        self.assertIn("Speaking with", text)
        self.assertIn("Natural (Alba)", text)

    def test_dry_run_does_not_save(self):
        code, _ = self.run_setup(["n", "3", "q", "q"], "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse((self.mind_dir / self.voice_engines.VOICE_SETTINGS).exists())

    def test_natural_voice_cannot_be_chosen_when_not_installed(self):
        self.piper.installed = False
        code, text = self.run_setup(["2", "q"], "--voice")
        self.assertIn("Not installed in this image", text)
        self.assertNotIn("Play the natural voice", text)
        self.assertFalse((self.mind_dir / self.voice_engines.VOICE_SETTINGS).exists())

    def test_appliance_restarts_the_voice_to_apply_it(self):
        with (patch.object(setup, "installed", return_value=["being-voice.service"]),
              patch.object(setup.subprocess, "run") as run):
            run.return_value.returncode = 0
            self.run_setup(["3", "q"], "--voice", appliance=True)
        self.assertEqual(run.call_args.args[0], ["systemctl", "try-restart", "being-voice.service"])
        self.assertEqual(self.voice_engines.load_preference(self.mind_dir), "flite")

    def test_samples_and_measuring(self):
        with (patch.object(setup, "engine_sample", return_value="I'm Pip.") as sample_call,
              patch.object(setup, "play_preview", new=AsyncMock(return_value=True)),
              patch.object(setup, "measure_natural_voice") as measure):
            self.run_setup(["n", "o", "m", "q"], "--voice")
        self.assertEqual([call.args[1] for call in sample_call.call_args_list], ["piper", "flite"])
        measure.assert_called_once_with(self.mind_dir)

    def test_a_voice_that_fails_does_not_end_setup(self):
        with patch.object(setup, "engine_sample", side_effect=RuntimeError("no voice")):
            code, text = self.run_setup(["n", "q"], "--voice")
        self.assertEqual(code, 0)
        self.assertIn("Could not play that voice: no voice", text)

    def test_measuring_records_the_result(self):
        class Piper:
            def __init__(self, *args, **kwargs):
                self.voice_dir = Path("/models/voice/piper/default")

            def label(self):
                return "Alba"

            def close(self):
                pass

        with (patch("mind.tts_sherpa.PiperEngine", Piper),
              patch.object(self.voice_engines, "measure_tts", return_value={"rtf": 0.3, "runs": [0.3] * 3}),
              redirect_stdout(io.StringIO())):
            entry = setup.measure_natural_voice(self.mind_dir)
        self.assertEqual(entry["rtf"], 0.3)
        self.assertIsNotNone(self.voice_engines.cached(self.mind_dir, "tts", "b1|piper:default:alba"))

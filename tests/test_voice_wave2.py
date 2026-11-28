"""The voice's wave 2 hooks: microphone cost, the typing hint, voice.level,
reply timing, engine choice and the unit's CPU and watchdog lines."""

import asyncio
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mind.voice as voice_module
from mind.mic_channel import ChannelPicker
from mind.recognition import FRAME_SAMPLES, MicLevel, amplify, levels, pcm
from mind.tts import FliteEngine
from mind.type_chat import typing_hint
from mind.voice import (
    APOLOGY, HINT_INTERVAL_SECONDS, HINT_WINDOW_SECONDS, SETTINGS_FILE, Voice, load_engine_choice,
    load_voice_settings, pick_tts, save_voice_settings,
)
from tests.test_voice_feedback import SILENCE, SPEECH, START, WAKE, Harness, real_sleep, tone

REPO = Path(__file__).resolve().parents[1]
HINT = "You can also type to me: press Mission Control, or fn and F2, then 6."


def write_wav(path, frames=160, rate=16000):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x01" * frames)


class MicrophoneCost(unittest.TestCase):
    def test_the_gain_costs_well_under_a_frame_on_the_host(self):
        quiet = struct.pack(f"<{FRAME_SAMPLES}h", *([10, -10] * (FRAME_SAMPLES // 2)))
        mic = MicLevel()
        for _ in range(500):  # settle the lift on a quiet floor
            mic.observe(*levels(pcm(quiet)), False)
        self.assertGreater(mic.gain, 1.0)
        frames = 500
        started = time.perf_counter()
        for _ in range(frames):
            samples = pcm(quiet)
            mic.observe(*levels(samples), False)
            amplify(samples, mic.gain)
        each = (time.perf_counter() - started) / frames
        self.assertLessEqual(each, 0.6e-3, f"{each * 1e6:.0f} us per 30 ms frame")

    def test_the_cost_including_the_channel_choice_is_logged_once(self):
        harness = Harness([], channel=ChannelPicker("auto"))
        stereo = struct.pack(f"<{FRAME_SAMPLES * 2}h", *([200, -100] * FRAME_SAMPLES))

        async def run():
            async with asyncio.TaskGroup() as harness.voice.tasks:
                harness.voice.capture = harness.voice.tasks.create_task(harness.voice.listen())
                await harness.frames(*[stereo] * 7)
                harness.voice.disarm()

        with patch("mind.voice.COST_FRAMES", 5), self.assertLogs("being.voice", "INFO") as logs:
            asyncio.run(run())
        lines = [line for line in logs.output if "microphone gain and channel cost" in line]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertRegex(lines[0], r"cost \d+ us per 30 ms frame \([\d.]+% of one core\) over 5 frames")


class TypingHint(unittest.TestCase):
    def voice(self, hint=lambda: HINT):
        clock = SimpleNamespace(now=1000.0)
        voice = Voice(Mock(), "Richard", [], "", clock=lambda: clock.now, typing_hint=hint)
        return voice, clock

    def test_a_second_miss_within_ten_minutes_adds_the_hint_once_a_day(self):
        voice, clock = self.voice()
        self.assertEqual(voice.apology_line(), APOLOGY)
        clock.now += HINT_WINDOW_SECONDS - 1
        self.assertEqual(voice.apology_line(), f"{APOLOGY} {HINT}")
        clock.now += 60
        self.assertEqual(voice.apology_line(), APOLOGY, "the hint is given once a day")
        clock.now += HINT_INTERVAL_SECONDS
        self.assertEqual(voice.apology_line(), APOLOGY, "a day later, but the miss before was long ago")
        clock.now += 60
        self.assertEqual(voice.apology_line(), f"{APOLOGY} {HINT}")

    def test_a_miss_long_after_the_last_gets_no_hint(self):
        voice, clock = self.voice()
        voice.apology_line()
        clock.now += HINT_WINDOW_SECONDS + 1
        self.assertEqual(voice.apology_line(), APOLOGY)

    def test_no_hint_where_no_menu_offers_typing(self):
        for hint in (None, lambda: None, Mock(side_effect=RuntimeError("no setup"))):
            with self.subTest(hint=hint):
                voice, clock = self.voice(hint)
                voice.apology_line()
                clock.now += 5
                with patch("builtins.print"):
                    self.assertEqual(voice.apology_line(), APOLOGY)

    def test_the_hint_names_the_try_menu_entry_or_setups_own_key(self):
        self.assertEqual(typing_hint(True), HINT)
        with patch("mind.type_chat.installed_key", return_value=None):
            self.assertIsNone(typing_hint(False))
        with patch("mind.type_chat.installed_key", return_value="Y"):
            self.assertEqual(typing_hint(False),
                             "You can also type to me: press Mission Control, or fn and F2, then Y.")


class ApologyReview(unittest.IsolatedAsyncioTestCase):
    async def test_a_wake_answered_with_words_owes_no_apology_for_a_later_cough(self):
        heard = (False, [("hear.final", {"text": "what time is it"})])
        miss = (False, [("hear.cancel", {})])
        cough = [SPEECH] * 20   # 0.6 s of sound, over MISSED_MIN_SECONDS
        harness = Harness([WAKE, START, SPEECH, heard, START, *cough, miss])
        voice = harness.voice
        voice.state = "attending"
        voice.apologise = AsyncMock()
        with patch("builtins.print"):
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(500), tone(4000), tone(4000), tone(10))
                self.assertFalse(voice.apology_owed)
                # The follow-up window: 0.5 s or more of sound with no words.
                await harness.frames(*[tone(4000)] * 21, tone(10))
                voice.disarm()
        self.assertIn(("hear.missed", {"reason": "unclear"}), harness.sent)
        voice.apologise.assert_not_called()

    async def test_the_typing_hint_is_worded_off_the_event_loop_and_kept(self):
        loop_thread = threading.current_thread()
        calls = []

        def hint():
            calls.append(threading.current_thread() is loop_thread)
            return HINT

        harness = Harness([], typing_hint=hint)
        voice = harness.voice
        voice.synthesize = AsyncMock()
        voice.play = AsyncMock()
        voice.state = "attending"
        with patch("builtins.print"):
            await voice.apologise()
            harness.clock.now += 60
            await voice.apologise()
            harness.clock.now += HINT_INTERVAL_SECONDS + 1
            await voice.apologise()
            harness.clock.now += 60
            await voice.apologise()
        lines = [args.args[0] for args in voice.synthesize.await_args_list]
        self.assertEqual(lines, [APOLOGY, f"{APOLOGY} {HINT}", APOLOGY, f"{APOLOGY} {HINT}"])
        self.assertEqual(calls, [False], "worded once, never on the event loop")


class VoiceLevel(unittest.IsolatedAsyncioTestCase):
    async def test_levels_are_numbers_and_engine_names_never_words(self):
        script = [(False, []), (False, []), START, SPEECH,
                  (False, [("hear.final", {"text": "remember my code is 4321"})])]
        harness = Harness(script)
        voice = harness.voice
        voice.state = "attending"
        with patch("builtins.print"):
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(30), tone(30), tone(5000), tone(5000), tone(30))
                voice.disarm()
        harness.clock.now += 12
        voice.lid_open = True
        await harness.event("voice.level_request")
        topic, fields = harness.sent[-1]
        self.assertEqual(topic, "voice.level")
        self.assertEqual(set(fields), {"floor_dbfs", "speech_peak_dbfs", "gain", "heard_ago_s", "asr", "tts"})
        self.assertEqual(fields["heard_ago_s"], 12)
        self.assertAlmostEqual(fields["speech_peak_dbfs"], -16.3, places=1)
        self.assertLess(fields["floor_dbfs"], -50)
        self.assertEqual((fields["asr"], fields["tts"]), ("pocketsphinx", "flite"))
        for name in ("floor_dbfs", "speech_peak_dbfs", "gain", "heard_ago_s"):
            self.assertIsInstance(fields[name], (int, float))
        self.assertNotIn("4321", json.dumps(fields))
        self.assertNotIn("remember", json.dumps(fields))

    async def test_nothing_measured_yet_is_none_not_a_guess(self):
        voice = Voice(Mock(), "Richard", [], "")
        fields = voice.level_fields()
        self.assertEqual((fields["floor_dbfs"], fields["speech_peak_dbfs"], fields["heard_ago_s"]), (None, None, None))
        self.assertEqual(fields["gain"], 1.0)

    async def test_levels_go_out_every_period_only_while_resting(self):
        harness = Harness()
        voice = harness.voice
        with patch("mind.voice.LEVEL_SECONDS", 0.01):
            loop = asyncio.create_task(voice.level_loop())
            async with asyncio.timeout(1):
                while "voice.level" not in harness.topics():
                    await asyncio.sleep(0.005)
            voice.state = "speaking"
            harness.sent.clear()
            await asyncio.sleep(0.05)
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)
        self.assertEqual(harness.sent, [])


class ReplyTiming(unittest.IsolatedAsyncioTestCase):
    async def test_one_timing_line_from_the_end_of_speech_to_the_first_audio(self):
        script = [(False, []), (False, []), (False, []), START, SPEECH, SPEECH,
                  (False, [("hear.final", {"text": "what time is it"})])]
        harness = Harness(script)
        voice = harness.voice
        voice.state = "attending"
        with self.assertLogs("being.voice", "INFO") as logs, tempfile.TemporaryDirectory() as work:
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(20), tone(20), tone(20), tone(4000), tone(4000))
                harness.clock.now += 0.3
                await harness.frames(SILENCE)   # still inside the endpointer's window, but quiet
                harness.clock.now += 0.3
                await harness.frames(SILENCE)   # the final
                voice.disarm()
            harness.clock.now += 0.9
            audio = Path(work) / "clause.wav"
            write_wav(audio)
            voice.lid_open = True

            @asynccontextmanager
            async def fake_child(*command, **options):
                yield Mock(returncode=0, wait=AsyncMock(return_value=0))

            with patch("mind.voice.child", fake_child):
                await voice.play_once(audio, True)
                await voice.play_once(audio, True)
        lines = [line for line in logs.output if "voice timing" in line]
        self.assertEqual(len(lines), 1, "one line per reply")
        timing = json.loads(lines[0].split("voice timing ", 1)[1])
        self.assertEqual(timing, {"eos_to_final_s": 0.6, "final_to_audio_s": 0.9, "eos_to_audio_s": 1.5})

    async def test_no_line_without_words_or_for_a_late_reply(self):
        voice = Voice(Mock(), "Richard", [], "")
        with self.assertNoLogs("being.voice", "INFO"):
            voice.first_audio()
            voice.pending_timing = (time.monotonic() - 100, time.monotonic() - 90)
            voice.first_audio()


class SyncEngine:
    name = "piper"

    def __init__(self, fail=False):
        self.fail = fail
        self.threads = []

    def synthesize(self, text, wav, rate):
        self.threads.append(threading.current_thread().name)
        if self.fail:
            raise RuntimeError("the natural voice produced no audio")
        write_wav(wav)


class AsyncEngine:
    name = "piper"

    def __init__(self):
        self.calls = []

    async def synthesize(self, text, wav, rate=1.0):
        self.calls.append((text, rate))
        write_wav(wav)


class Engines(unittest.IsolatedAsyncioTestCase):
    async def test_a_loaded_engine_speaks_in_one_worker_thread_of_its_own(self):
        engine = SyncEngine()
        voice = Voice(Mock(), "Richard", [], "", tts=engine, rate=0.9)
        with tempfile.TemporaryDirectory() as work:
            for index in range(3):
                await voice.synthesize("Hello there.", Path(work) / f"{index}.wav", False)
        self.assertEqual(len(set(engine.threads)), 1)
        self.assertTrue(engine.threads[0].startswith("voice-tts"))
        self.assertNotEqual(engine.threads[0], threading.current_thread().name)
        voice._synthesis_pool.shutdown()

        engine = AsyncEngine()
        voice = Voice(Mock(), "Richard", [], "", tts=engine, rate=0.9)
        with tempfile.TemporaryDirectory() as work:
            await voice.synthesize("Hello.", Path(work) / "a.wav", False)
        self.assertEqual(engine.calls, [("Hello.", 0.9)])

    async def test_an_engine_error_falls_back_to_flite_for_that_clause(self):
        voice = Voice(Mock(), "Richard", [], "", tts=SyncEngine(fail=True))
        commands = []

        @asynccontextmanager
        async def fake_child(*command, **options):
            commands.append(command)
            yield Mock(wait=AsyncMock(return_value=0))

        with tempfile.TemporaryDirectory() as work, patch("mind.voice.child", fake_child), \
                self.assertLogs("being.voice", "WARNING") as logs:
            await voice.synthesize("Hello.", Path(work) / "a.wav", True)
        self.assertEqual(commands[0][0], "flite")
        self.assertEqual(commands[0][-3], "Hello.")
        self.assertIn("Flite says it instead", logs.output[0])
        self.assertEqual(voice.tts.name, "piper", "the next clause tries the chosen engine again")
        voice._synthesis_pool.shutdown()

    async def test_voice_engine_is_kept_in_voice_json_and_version_one_still_reads(self):
        with tempfile.TemporaryDirectory() as mind:
            path = Path(mind) / SETTINGS_FILE
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"version": 1, "volume": 40, "chirps": False}))
            self.assertEqual(load_voice_settings(mind), {"volume": 40, "chirps": False})
            self.assertEqual(load_engine_choice(mind), "auto")

            voice = Voice(Mock(), "Richard", [], "", settings_dir=Path(mind),
                          settings=load_voice_settings(mind))
            voice.bus.send = AsyncMock()
            voice.bus.connected.set()
            await voice.handle({"t": "voice.engine", "tts": "flite"})
            await voice.engine_task
            saved = json.loads(path.read_text())
            self.assertEqual(saved, {"version": 2, "volume": 40, "chirps": False, "tts": "flite"})
            self.assertEqual(load_engine_choice(mind), "flite")
            self.assertEqual(load_voice_settings(mind), {"volume": 40, "chirps": False})
            # The new engine is reported as a level, numbers and names only.
            self.assertEqual(voice.bus.send.await_args.args[0], "voice.level")
            self.assertEqual(voice.bus.send.await_args.kwargs["tts"], "flite")

            with patch("builtins.print"):
                await voice.handle({"t": "voice.engine", "tts": "loud"})
            self.assertEqual(load_engine_choice(mind), "flite")
            save_voice_settings(mind, volume=None, chirps=True, tts="nonsense")
            self.assertEqual(load_engine_choice(mind), "auto")

    async def test_the_choice_comes_from_voice_engines_when_it_is_installed(self):
        piper = AsyncEngine()
        engines = SimpleNamespace(choose_tts=Mock(return_value=(piper, "Using the natural voice.")))
        self.assertEqual(pick_tts(engines, "auto", Path("/mind")), (piper, "Using the natural voice."))
        engines.choose_tts.assert_called_once_with("auto", state_dir=Path("/mind"))
        engine, _ = pick_tts(engines, "flite", Path("/mind"))
        self.assertIsInstance(engine, FliteEngine)
        broken = SimpleNamespace(choose_tts=Mock(side_effect=OSError("no library")))
        engine, note = pick_tts(broken, "auto", Path("/mind"))
        self.assertIsInstance(engine, FliteEngine)
        self.assertIn("Using Flite", note)
        engine, _ = pick_tts(None, "auto", Path("/mind"))
        self.assertIsInstance(engine, FliteEngine)

    async def test_the_recognizer_is_swapped_at_rest_and_a_spare_wake_spotter_is_freed(self):
        for composed in (True, False):
            with self.subTest(composed=composed):
                wake = Mock()
                voice = Voice(wake, "Richard", [], "")
                voice.state = "idle"
                engine = SimpleNamespace(wake=wake) if composed else SimpleNamespace(name="other")
                with patch("builtins.print"):
                    await voice.adopt_recognizer(engine, wake)
                self.assertIs(voice.recognizer, engine)
                self.assertEqual(voice.asr, "moonshine" if composed else "other")
                self.assertEqual(wake.close.called, not composed)

    async def test_engines_are_chosen_off_the_event_loop_at_start(self):
        piper = AsyncEngine()
        wake = Mock(wake_phrase="Hey Pip")
        sherpa = SimpleNamespace(wake=wake)
        loop_thread = threading.current_thread()
        seen = []

        def choose_tts(choice, *, state_dir):
            seen.append(("tts", choice, threading.current_thread() is loop_thread))
            return piper, None

        def choose_asr(wake_, *, state_dir, wake_word):
            seen.append(("asr", wake_word, threading.current_thread() is loop_thread))
            return sherpa, "Using Moonshine after the wake phrase."

        engines = SimpleNamespace(choose_tts=choose_tts, choose_asr=choose_asr)
        voice = Voice(wake, "Richard", [], "", engines=engines, tts_choice="auto", settings_dir=Path("/unused"))
        voice.state = "idle"
        with patch("mind.voice.ENGINE_REFRESH_SECONDS", 3600), patch("builtins.print"):
            task = asyncio.create_task(voice.choose_engines())
            async with asyncio.timeout(1):
                while voice.recognizer is not sherpa:
                    await real_sleep(0.001)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIs(voice.tts, piper)
        self.assertEqual(seen, [("tts", "auto", False), ("asr", "Hey Pip", False)])
        self.assertEqual(voice.level_fields()["asr"], "moonshine")
        self.assertEqual(voice.level_fields()["tts"], "piper")
        wake.close.assert_not_called()

    async def test_a_voice_that_will_not_load_keeps_flite_and_still_gets_the_better_ears(self):
        class Broken(AsyncEngine):
            closed = False

            async def warm(self):
                raise OSError("no espeak-ng-data")

            def close(self):
                self.closed = True

        broken = Broken()
        wake = Mock(wake_phrase="Hey Pip")
        sherpa = SimpleNamespace(wake=wake)
        engines = SimpleNamespace(choose_tts=Mock(return_value=(broken, None)),
                                  choose_asr=Mock(return_value=(sherpa, None)))
        voice = Voice(wake, "Richard", [], "", engines=engines, settings_dir=Path("/unused"))
        voice.state = "idle"
        with patch("mind.voice.ENGINE_REFRESH_SECONDS", 3600), patch("builtins.print"), \
                self.assertLogs("being.voice", "WARNING") as logs:
            task = asyncio.create_task(voice.choose_engines())
            async with asyncio.timeout(1):
                while voice.recognizer is not sherpa:
                    await real_sleep(0.001)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(voice.tts, FliteEngine)
        self.assertTrue(broken.closed, "the half-loaded voice gives its memory back")
        self.assertIn("could not load the piper voice", logs.output[0])

    async def test_a_forced_voice_is_kept_but_the_ears_are_still_chosen(self):
        wake = Mock(wake_phrase="Hey Pip")
        sherpa = SimpleNamespace(wake=wake)
        forced = SyncEngine()
        engines = SimpleNamespace(choose_tts=Mock(), choose_asr=Mock(return_value=(sherpa, None)))
        voice = Voice(wake, "Richard", [], "", tts=forced, engines=engines, tts_forced=True,
                      settings_dir=Path("/unused"))
        voice.state = "idle"
        with patch("mind.voice.ENGINE_REFRESH_SECONDS", 3600), patch("builtins.print"):
            task = asyncio.create_task(voice.choose_engines())
            async with asyncio.timeout(1):
                while voice.recognizer is not sherpa:
                    await real_sleep(0.001)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        engines.choose_tts.assert_not_called()
        self.assertIs(voice.tts, forced)

    async def test_a_replaced_voice_is_closed_on_the_thread_that_speaks_with_it(self):
        class Closing(SyncEngine):
            def close(self):
                self.threads.append(("close", threading.current_thread().name))

        old = Closing()
        voice = Voice(Mock(), "Richard", [], "", tts=old)
        with tempfile.TemporaryDirectory() as work:
            await voice.synthesize("Hello.", Path(work) / "a.wav", False)
        with patch("builtins.print"):
            await voice.use_tts(AsyncEngine(), "piper")
        speaking = old.threads[0]
        self.assertEqual(old.threads[-1], ("close", speaking))
        voice._synthesis_pool.shutdown()


class VoiceUnit(unittest.TestCase):
    def test_cpu_weight_watchdog_and_compiled_modules(self):
        unit = (REPO / "os/package/being/being-voice.service").read_text()
        lines = [line.strip() for line in unit.splitlines()]
        self.assertIn("CPUWeight=1000", lines)
        self.assertIn("WatchdogSec=30s", lines)
        self.assertIn("NotifyAccess=main", lines)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", unit)
        # The watchdog is fed from the voice's own loop.
        self.assertIn("sd_notify.keep_alive()", (REPO / "mind/voice.py").read_text())

    def test_the_organ_pings_systemd_while_it_runs(self):
        async def run():
            voice = Voice(Mock(), "Richard", [], "")
            voice.bus.start = AsyncMock()
            voice.bus.stop = AsyncMock()
            received = asyncio.Event()

            async def recv():
                await received.wait()
                raise asyncio.CancelledError

            voice.bus.recv = recv
            pings = []
            with patch("mind.voice.sd_notify.notify", side_effect=lambda message, **_: pings.append(message)):
                task = asyncio.create_task(voice.run())
                async with asyncio.timeout(1):
                    while "WATCHDOG=1" not in pings:
                        await asyncio.sleep(0.001)
                received.set()
                await asyncio.gather(task, return_exceptions=True)
            return pings

        pings = asyncio.run(run())
        self.assertEqual(pings[:2], ["READY=1", "WATCHDOG=1"])


if __name__ == "__main__":
    unittest.main()


class RingBell(unittest.IsolatedAsyncioTestCase):
    """A ring's first say.request carries chirp="bell": the bell plays before its words."""

    async def speak(self, *clauses, **state):
        from contextlib import nullcontext

        voice = Voice(Mock(), "Richard", [], "")
        voice.chirps_on, voice.lid_open, voice.muted = True, True, False
        for key, value in state.items():
            setattr(voice, key, value)
        voice.chirp_bank = Mock(path=Mock(return_value="/unused/bell.wav"))
        order = []
        voice.play_chirp = AsyncMock(side_effect=lambda path: order.append(("bell", path)) or True)
        voice.play = AsyncMock(side_effect=lambda audio, first: order.append(("words", first)))
        voice.send_audio = AsyncMock()
        voice.synthesize = AsyncMock()
        for clause in clauses:
            voice.clauses.put_nowait(voice_module.clause_request(clause))
        with patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 2)
        return order

    async def test_the_bell_plays_once_before_the_first_words(self):
        order = await self.speak({"text": "Your tea timer is done.", "last": False, "chirp": "bell"},
                                 {"text": "Shall I snooze it?", "last": True})
        self.assertEqual(order, [("bell", "/unused/bell.wav"), ("words", True), ("words", False)])

    async def test_no_bell_without_the_chirp_field_or_with_chirps_off_or_muted(self):
        plain = await self.speak({"text": "Hello.", "last": True})
        self.assertEqual(plain, [("words", True)])
        self.assertEqual(voice_module.clause_request({"text": "x", "last": True, "chirp": "ears"}).get("chirp"), None)
        for state in ({"chirps_on": False}, {"muted": True}, {"lid_open": False}):
            order = await self.speak({"text": "Your timer is done.", "last": True, "chirp": "bell"}, **state)
            self.assertEqual(order, [("words", True)], state)

    async def test_the_bell_is_only_for_the_first_clause_and_waits_for_no_quiet_hours(self):
        order = await self.speak({"text": "Hello.", "last": False}, {"text": "Again.", "last": True, "chirp": "bell"})
        self.assertEqual(order, [("words", True), ("words", False)], "a bell mid-reply never plays")
        # The mind already decided this ring may sound; the voice does not second-guess it.
        order = await self.speak({"text": "Your alarm.", "last": True, "chirp": "bell"}, quiet_hours=lambda clock: True)
        self.assertEqual(order[0], ("bell", "/unused/bell.wav"))

    async def test_a_ring_with_no_words_is_the_bell_alone_and_the_ring_is_reported(self):
        # Written chat: the words are shown on screen, the voice only chimes.
        voice = Voice(Mock(), "Richard", [], "")
        voice.chirps_on, voice.lid_open, voice.muted = True, True, False
        voice.chirp_bank = Mock(path=Mock(return_value="/unused/bell.wav"))
        order = []
        voice.play_chirp = AsyncMock(side_effect=lambda path: order.append("bell") or True)
        voice.play = AsyncMock(side_effect=lambda audio, first: order.append("words"))
        voice.send_audio = AsyncMock()
        voice.clauses.put_nowait(voice_module.clause_request(
            {"text": "", "seq": 1, "last": True, "chirp": "bell", "announcement": "tok"}))
        from contextlib import nullcontext
        with patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 2)
        self.assertEqual(order, ["bell"])
        done = [c for c in voice.send_audio.await_args_list if c.args[0] == "say.done"]
        self.assertEqual(done[-1].kwargs, {"final": True, "announcement": "tok"})

    async def test_no_bell_when_muted_for_a_wordless_ring(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.chirps_on, voice.lid_open, voice.muted = True, True, True
        voice.chirp_bank = Mock(path=Mock(return_value="/unused/bell.wav"))
        voice.play_chirp = AsyncMock(return_value=True)
        voice.send_audio = AsyncMock()
        voice.clauses.put_nowait(voice_module.clause_request({"text": "", "seq": 1, "last": True, "chirp": "bell"}))
        from contextlib import nullcontext
        with patch("mind.voice.tempfile.TemporaryDirectory", return_value=nullcontext("/unused")):
            await asyncio.wait_for(voice.speak(), 2)
        voice.play_chirp.assert_not_awaited()

    async def test_a_chirp_still_playing_stops_before_the_bell(self):
        voice = Voice(Mock(), "Richard", [], "")
        voice.chirps_on, voice.lid_open, voice.muted = True, True, False
        voice.chirp_bank = Mock(path=Mock(return_value="/unused/bell.wav"))
        purr = asyncio.create_task(asyncio.sleep(60))
        voice.chirp_task = purr
        voice.play_chirp = AsyncMock(return_value=True)
        self.assertTrue(await voice.ring_bell())
        self.assertTrue(purr.cancelled(), "never two sounds at once")
        voice.muted = True
        self.assertFalse(await voice.ring_bell())
        self.assertEqual(voice.play_chirp.await_count, 1)


class Loading(AsyncEngine):
    """A natural voice whose load is held until released; records its close."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.closed = threading.Event()

    async def warm(self):
        await self.release.wait()

    def close(self):
        self.closed.set()


class EngineRaces(unittest.IsolatedAsyncioTestCase):
    """The owner's spoken voice choice beats a measurement or start-up load."""

    async def wait_for(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await real_sleep(0.001)

    async def test_a_measurement_finishing_after_a_spoken_choice_does_not_override_it(self):
        piper = Loading()
        piper.release.set()
        measuring = asyncio.Event()
        finish = asyncio.Event()

        async def refresh_tts(current, choice, *, state_dir):
            measuring.set()
            await finish.wait()     # three sentences take seconds on the laptop
            return piper, "The natural voice is fast enough now."

        with tempfile.TemporaryDirectory() as work:
            engines = SimpleNamespace(refresh_tts=refresh_tts)
            wake = Mock()
            voice = Voice(wake, "Richard", [], "", engines=engines, settings_dir=Path(work))
            voice.state = "idle"
            with patch("builtins.print"), patch.object(voice, "publish_level", AsyncMock()):
                refresh = asyncio.create_task(voice.refresh_engines(wake))
                await measuring.wait()
                # "Hey Pip, use your original voice."
                await voice.change_engine("flite")
                await voice.engine_task
                chosen = voice.tts
                finish.set()
                await refresh
            self.assertIs(voice.tts, chosen)
            self.assertIsInstance(voice.tts, FliteEngine)
            self.assertEqual(voice.tts_choice, "flite")
            await self.wait_for(piper.closed.is_set)

    async def test_a_start_up_voice_still_loading_does_not_override_a_spoken_choice(self):
        piper = Loading()
        wake = Mock(wake_phrase="Hey Pip")
        with tempfile.TemporaryDirectory() as work:
            engines = SimpleNamespace(choose_tts=Mock(return_value=(piper, None)),
                                      choose_asr=Mock(return_value=(wake, None)))
            voice = Voice(wake, "Richard", [], "", engines=engines, settings_dir=Path(work))
            voice.state = "idle"
            with patch("mind.voice.ENGINE_REFRESH_SECONDS", 3600), patch("builtins.print"), \
                    patch.object(voice, "publish_level", AsyncMock()):
                task = asyncio.create_task(voice.choose_engines())
                await self.wait_for(lambda: engines.choose_tts.called)
                await real_sleep(0.01)
                await voice.change_engine("flite")
                await voice.engine_task
                chosen = voice.tts
                piper.release.set()
                await self.wait_for(lambda: engines.choose_asr.called)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertIs(voice.tts, chosen)
            self.assertIsInstance(voice.tts, FliteEngine)
            await self.wait_for(piper.closed.is_set)

    async def test_a_voice_change_cancelled_while_loading_gives_the_voice_back(self):
        first, second = Loading(), Loading()
        second.release.set()
        picks = iter([(first, None), (second, None)])
        with tempfile.TemporaryDirectory() as work:
            engines = SimpleNamespace(choose_tts=Mock(side_effect=lambda *a, **k: next(picks)))
            voice = Voice(Mock(), "Richard", [], "", engines=engines, settings_dir=Path(work))
            with patch("builtins.print"), patch.object(voice, "publish_level", AsyncMock()):
                await voice.change_engine("piper")      # "use your natural voice"
                await self.wait_for(lambda: engines.choose_tts.call_count == 1)
                await real_sleep(0.01)
                await voice.change_engine("auto")       # changed again while it loads
                await voice.engine_task
            self.assertIs(voice.tts, second)
            await self.wait_for(first.closed.is_set)
            self.assertFalse(second.closed.is_set())

    async def test_a_measured_voice_is_still_adopted_when_nothing_changed(self):
        piper = Loading()
        piper.release.set()

        async def refresh_tts(current, choice, *, state_dir):
            return piper, None

        voice = Voice(Mock(), "Richard", [], "", engines=SimpleNamespace(refresh_tts=refresh_tts),
                      settings_dir=Path("/unused"))
        with patch("builtins.print"):
            await voice.refresh_engines(Mock())
        self.assertIs(voice.tts, piper)
        self.assertFalse(piper.closed.is_set())


class WakeDuringTurn(unittest.IsolatedAsyncioTestCase):
    async def test_a_wake_heard_while_thinking_owes_no_apology_for_a_cough_after_the_reply(self):
        heard = (False, [("hear.final", {"text": "what's the weather"})])
        miss = (False, [("hear.cancel", {})])
        cough = [SPEECH] * 20   # 0.6 s of sound, over MISSED_MIN_SECONDS
        harness = Harness([WAKE, START, SPEECH, heard, WAKE, START, *cough, miss])
        voice = harness.voice
        voice.state = "attending"
        voice.apologise = AsyncMock()
        with patch("builtins.print"):
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(500), tone(4000), tone(4000), tone(10))
                voice.state = "thinking"
                await harness.frames(tone(500))      # "Hey Pip" again, or the TV
                self.assertIn(("hear.wake", {}), harness.sent)
                self.assertFalse(voice.apology_owed)
                self.assertFalse(voice.expecting)
                voice.state = "attending"            # the follow-up window after the reply
                await harness.frames(*[tone(4000)] * 21, tone(10))
                voice.disarm()
        self.assertIn(("hear.missed", {"reason": "unclear"}), harness.sent)
        voice.apologise.assert_not_called()

    async def test_a_wake_at_rest_still_expects_words(self):
        harness = Harness([WAKE])
        voice = harness.voice
        voice.state = "idle"
        with patch("builtins.print"):
            async with asyncio.TaskGroup() as voice.tasks:
                voice.capture = voice.tasks.create_task(voice.listen())
                await harness.frames(tone(500))
                self.assertTrue(voice.apology_owed)
                self.assertTrue(voice.expecting)
                voice.disarm()

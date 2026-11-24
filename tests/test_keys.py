"""The mind's answers to the keyboard's top row (mind/keys.py)."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from mind import app_commands, keys
from tests.test_mind import MindCase
from tests.test_turn_apps import Hands


class FakeBus:
    def __init__(self):
        self.sent = []

    async def send(self, topic, **fields):
        self.sent.append((topic, fields))


class FakeApps:
    """The workshop's AppBridge, answering at once."""

    def __init__(self, terminal="closed", hands_up=True, guest=False):
        self.view = {"terminal": terminal}
        self.hands_up = hands_up
        self.guest = guest
        self.handled = []

    def parse(self, text, guest=None):
        ctx = app_commands.AppContext(hands_up=self.hands_up, guest=self.guest if guest is None else guest,
                                      terminal=self.view["terminal"])
        return app_commands.parse(text, ctx)

    async def handle(self, command, say):
        self.handled.append(command)
        if command.kind == "say":
            await say(command.say)
        elif command.kind in ("open", "restore"):
            self.view["terminal"] = "shown"
        elif command.kind == "minimise":
            self.view["terminal"] = "minimised"


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.level = 70
        self.logged = []
        self.apps = FakeApps()
        self.guest = False
        work = tempfile.TemporaryDirectory(prefix="being-keys-")
        self.addCleanup(work.cleanup)
        self.state = Path(work.name)
        self.saved = []
        self.keys = self.make_keys()

    def make_keys(self, **extra):
        return keys.Keys(self.bus.send, volume=lambda: self.level, set_volume=self.set_volume,
                         apps=self.apps, guest=lambda: self.guest, state_dir=self.state,
                         log=self.logged.append, **extra)

    def set_volume(self, level):
        self.level = level
        self.saved.append(level)

    def wait(self, coroutine):
        return asyncio.run(coroutine)

    def marker(self):
        return (self.state / keys.MUTED_MARKER).exists()

    def press(self, topic, **fields):
        async def run():
            answered = await self.keys.handle({"t": topic, **fields})
            await asyncio.sleep(0)  # let a Launchpad press finish
            task = self.keys._app_task
            if task is not None:
                await task
            return answered
        return asyncio.run(run())

    def last(self):
        return self.bus.sent[-1]

    def test_only_its_own_topics_are_answered(self):
        self.assertFalse(self.press("key.mute"), "the microphone's mute is the brainstem's")
        self.assertFalse(self.press("key.interrupt"))
        self.assertFalse(self.press("hear.final", text="louder"))
        self.assertEqual(self.bus.sent, [])
        self.assertEqual(set(keys.TOPICS),
                         {"key.brightness", "key.volume", "key.mute_speaker", "key.kbd_light", "key.apps"})

    def test_brightness_keys_step_the_panel_through_the_brainstem(self):
        self.assertTrue(self.press("key.brightness", dir="up"))
        self.assertEqual(self.last(), ("being.brightness", {"step": 1, "by": keys.BRIGHTNESS_STEP, "req": "key"}))
        self.press("key.brightness", dir="down")
        self.assertEqual(self.last(), ("being.brightness", {"step": -1, "by": keys.BRIGHTNESS_STEP, "req": "key"}))
        self.assertTrue(1 <= keys.BRIGHTNESS_STEP <= 50, "the brainstem takes 1 to 50 points")
        self.press("key.brightness", dir="sideways")
        self.press("key.brightness")
        self.assertEqual(len(self.bus.sent), 2, "a strange direction changes nothing")
        self.assertEqual(len(self.logged), 2)

    def test_keyboard_light_keys_step_and_toggle(self):
        self.press("key.kbd_light", dir="up")
        self.assertEqual(self.last(), ("being.kbd_light", {"step": 1, "req": "key"}))
        self.press("key.kbd_light", dir="down")
        self.assertEqual(self.last(), ("being.kbd_light", {"step": -1, "req": "key"}))
        self.press("key.kbd_light", dir="toggle")
        self.assertEqual(self.last(), ("being.kbd_light", {"toggle": True, "req": "key"}))
        self.press("key.kbd_light", dir=3)
        self.assertEqual(len(self.bus.sent), 3)

    def test_volume_keys_step_in_sixteenths_with_a_click(self):
        self.level = 50
        self.press("key.volume", dir="up")
        self.assertEqual(self.last(), ("voice.volume", {"level": 56, "feedback": True}))
        self.assertEqual(self.level, 56, "the mind keeps the new level, as for \"louder\"")
        self.press("key.volume", dir="down")
        self.press("key.volume", dir="down")
        self.assertEqual(self.last()[1]["level"], 44)
        self.level = 100
        self.press("key.volume", dir="up")
        self.assertEqual(self.last()[1]["level"], 100, "already full: the bar still shows it")
        self.level = 6
        self.press("key.volume", dir="down")
        self.assertEqual(self.last(), ("voice.volume", {"level": 0, "persist": False}),
                         "the lowest step is silence, as on a Mac, and only for now")
        self.assertEqual(self.level, 6, "the saved level is still the one before the silence")
        self.assertTrue(self.keys.muted)
        self.press("key.volume", dir="down")
        self.assertEqual(self.last(), ("voice.volume", {"level": 0, "persist": False}), "it stays there")
        self.press("key.volume", dir="up")
        self.assertEqual(self.last(), ("voice.volume", {"level": 6, "feedback": True}), "and steps up from silence")
        self.assertFalse(self.keys.muted)
        self.press("key.volume", dir="down")
        self.press("key.mute_speaker")
        self.assertEqual(self.last()[1]["level"], 6, "the mute key brings back the quietest step")
        self.assertNotIn(0, self.saved, "silence is never kept as the owner's level")

    def test_the_sixteen_steps(self):
        levels = [0]
        for _ in range(keys.STEPS + 2):
            levels.append(keys.volume_step(levels[-1], True))
        self.assertEqual(levels[:keys.STEPS + 1],
                         [0, 6, 12, 19, 25, 31, 38, 44, 50, 56, 62, 69, 75, 81, 88, 94, 100])
        self.assertEqual(levels[-1], 100)
        for level in range(0, 101):
            up, down = keys.volume_step(level, True), keys.volume_step(level, False)
            self.assertTrue(0 <= down <= level <= up <= 100, level)
        self.assertEqual(keys.volume_step(70, True), 75, "a level between steps goes to the next one")
        self.assertEqual(keys.volume_step(-5, False), 0)
        self.assertEqual(keys.volume_step(140, True), 100)

    def test_mute_silences_the_speaker_and_brings_it_back(self):
        self.level = 62
        self.press("key.mute_speaker")
        self.assertEqual(self.last(), ("voice.volume", {"level": 0, "persist": False}),
                         "no click when muting, and the voice keeps its stored level")
        self.assertEqual(self.level, 62, "the mind keeps the level from before the mute")
        self.assertEqual(self.saved, [])
        self.assertTrue(self.keys.muted and self.marker())
        self.press("key.mute_speaker")
        self.assertEqual(self.last(), ("voice.volume", {"level": 62, "feedback": True}))
        self.assertEqual(self.level, 62)
        self.assertFalse(self.keys.muted or self.marker())
        # Volume keys while muted step from the level it was muted at.
        self.press("key.mute_speaker")
        self.press("key.volume", dir="up")
        self.assertEqual(self.last()[1]["level"], 69)
        self.press("key.mute_speaker")
        self.press("key.volume", dir="down")
        self.assertEqual(self.last()[1]["level"], 62)

    def test_a_silent_level_it_did_not_set_comes_back_at_half(self):
        self.level = 0  # left at nothing by something else
        self.press("key.mute_speaker")
        self.assertEqual(self.last()[1]["level"], keys.UNMUTE_DEFAULT)
        self.level = None  # no level known at all
        self.press("key.volume", dir="up")
        self.assertEqual(self.last()[1]["level"], keys.volume_step(keys.UNMUTE_DEFAULT, True))

    def test_the_voices_report_of_a_key_mute_is_not_the_owners_level(self):
        self.level = 50
        self.assertFalse(self.keys.volume_report(0), "not muted: a 0 is a 0")
        self.press("key.mute_speaker")
        self.assertTrue(self.keys.volume_report(0), "the mute's own silence is not kept")
        self.assertTrue(self.keys.muted)
        self.assertFalse(self.keys.volume_report("0"))
        # "Louder", said while muted, brings the sound back: the mute is over.
        self.assertFalse(self.keys.volume_report(60))
        self.assertFalse(self.keys.muted or self.marker())
        self.assertFalse(self.keys.volume_report(0))

    def test_a_ring_brings_a_muted_speaker_back_first(self):
        self.level = 75
        self.assertFalse(self.wait(self.keys.unmute("an alarm is ringing")), "not muted: nothing to do")
        self.assertEqual(self.bus.sent, [])
        self.press("key.mute_speaker")
        self.assertTrue(self.wait(self.keys.unmute("an alarm is ringing")))
        self.assertEqual(self.last(), ("voice.volume", {"level": 75}), "the saved level, with no click")
        self.assertFalse(self.keys.muted or self.marker())
        self.assertIn("alarm", self.logged[-1])
        # Stepped down to silence, it comes back at the quietest step.
        self.level = 6
        self.press("key.volume", dir="down")
        self.wait(self.keys.unmute("a timer is ringing"))
        self.assertEqual(self.last(), ("voice.volume", {"level": 6}))

    def test_a_mute_does_not_outlive_the_mind(self):
        self.level = 70
        self.press("key.mute_speaker")
        self.assertTrue(self.marker(), "a mute leaves its note in the state directory")
        # The laptop restarts while muted: the voice may have stored the 0,
        # the mind's own level is still 70, and the note is there.
        self.bus.sent.clear()
        after = self.make_keys()
        self.assertFalse(after.muted, "a new mind is never muted")
        self.wait(after.start())
        self.assertEqual(self.bus.sent, [("voice.volume", {"level": 70})])
        self.assertFalse(self.marker())
        self.wait(after.start())
        self.assertEqual(len(self.bus.sent), 1, "only once, and never without a note")
        fresh = keys.Keys(self.bus.send, volume=lambda: 70, set_volume=self.set_volume, log=self.logged.append)
        self.wait(fresh.start())
        self.assertEqual(len(self.bus.sent), 1, "no state directory: nothing to put back")

    def test_a_restart_with_an_unknown_level_comes_back_audible(self):
        self.level = 30
        self.press("key.mute_speaker")
        self.level = 0
        self.bus.sent.clear()
        self.wait(self.make_keys().start())
        self.assertEqual(self.bus.sent, [("voice.volume", {"level": keys.UNMUTE_DEFAULT})])

    def test_an_unwritable_state_directory_still_mutes(self):
        blocked = self.state / "file"
        blocked.write_text("")
        k = keys.Keys(self.bus.send, volume=lambda: 40, set_volume=self.set_volume, state_dir=blocked / "sub",
                      log=self.logged.append)
        self.assertTrue(self.wait(k.handle({"t": "key.mute_speaker"})))
        self.assertEqual(self.last(), ("voice.volume", {"level": 0, "persist": False}))
        self.assertIn("could not note", self.logged[-1])
        self.wait(k.handle({"t": "key.mute_speaker"}))
        self.assertEqual(self.last()[1]["level"], 40)

    def test_the_menu_key_is_named_as_the_laptop_has_it(self):
        self.assertIn("Mission Control", keys.MENU_KEYS)
        self.assertIn("fn and F2", keys.MENU_KEYS)

    def test_launchpad_opens_the_terminal_and_puts_it_away(self):
        self.press("key.apps")
        self.assertEqual([c.kind for c in self.apps.handled], ["open"])
        self.assertEqual(self.apps.view["terminal"], "shown")
        self.press("key.apps")
        self.assertEqual(self.apps.handled[-1].kind, "minimise")
        self.press("key.apps")
        self.assertEqual(self.apps.handled[-1].kind, "restore")
        self.assertEqual(self.bus.sent, [], "the workshop is asked through its own bridge")
        self.assertEqual(self.logged, [])

    def test_launchpad_refusals_are_logged_not_spoken(self):
        self.guest = True
        self.press("key.apps")
        self.assertEqual(self.apps.view["terminal"], "closed")
        self.assertEqual(self.logged, ["Launchpad: My terminal stays closed while a guest is here."])
        self.guest = False
        self.apps.hands_up = False
        self.press("key.apps")
        self.assertIn("hands", self.logged[-1])
        no_workshop = keys.Keys(self.bus.send, volume=lambda: 50, set_volume=self.set_volume,
                                log=self.logged.append)
        asyncio.run(no_workshop.handle({"t": "key.apps"}))
        self.assertIn("no workshop", self.logged[-1])

    def test_a_failing_bus_or_workshop_never_stops_the_mind(self):
        async def broken(topic, **fields):
            raise OSError("bus gone")

        k = keys.Keys(broken, volume=lambda: 50, set_volume=self.set_volume, log=self.logged.append)
        self.assertTrue(asyncio.run(k.handle({"t": "key.volume", "dir": "up"})))
        self.assertIn("key.volume failed", self.logged[-1])

        class Hangs(FakeApps):
            async def handle(self, command, say):
                await asyncio.sleep(3600)

        async def run():
            slow = keys.Keys(self.bus.send, volume=lambda: 50, set_volume=self.set_volume, apps=Hangs(),
                             log=self.logged.append)
            await slow.handle({"t": "key.apps"})
            await slow.handle({"t": "key.apps"})
            await slow.close()
            return slow
        asyncio.run(run())
        self.assertIn("still being answered", self.logged[-1])


def _hooked():
    """Whether mind/turn.py has the top-row hook yet (it is another file's)."""
    from mind import turn

    return set(keys.TOPICS) <= set(turn.SUBS)


@unittest.skipUnless(_hooked(), "mind/turn.py does not answer the top-row keys yet")
class KeysWithTheTurnLoop(MindCase):
    """The keys through the real mind, as the brainstem passes them on."""

    async def test_keys_reach_the_brainstem_and_the_voice(self):
        self.bus.push("voice.volume_state", level=50, mixer="hardware")
        self.bus.push("key.volume", dir="up")
        sent = await self.bus.wait("voice.volume")
        self.assertEqual((sent["level"], sent["feedback"]), (56, True))
        self.assertEqual(self.mind._volume, 56)
        self.bus.push("key.brightness", dir="down")
        sent = await self.bus.wait("being.brightness")
        self.assertEqual((sent["step"], sent["by"]), (-1, keys.BRIGHTNESS_STEP))
        self.bus.push("key.kbd_light", dir="toggle")
        self.assertEqual((await self.bus.wait("being.kbd_light"))["toggle"], True)
        self.bus.push("key.mute_speaker")
        self.assertEqual((await self.bus.wait("voice.volume", 2))["level"], 0)
        self.assertEqual(self.bus.of("say.request"), [], "no key speaks")

    async def test_a_key_mute_is_never_kept_and_ends_with_the_mind(self):
        self.bus.push("voice.volume_state", level=62, mixer="hardware")
        self.bus.push("key.mute_speaker")
        sent = await self.bus.wait("voice.volume")
        self.assertEqual((sent["level"], sent.get("persist")), (0, False))
        self.bus.push("voice.volume_state", level=0, mixer="hardware")  # the voice's report of it
        for _ in range(100):
            if self.mind._keys.muted:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertEqual(self.mind._volume, 62, "the silence is not the owner's level")
        # The laptop restarts while muted: the mind puts the sound back.
        await self.stop_mind()
        await self.start_mind()
        self.assertEqual((await self.bus.wait("voice.volume"))["level"], 62)
        self.assertFalse(self.mind._keys.muted)

    async def test_a_ring_unmutes_a_muted_speaker(self):
        self.bus.push("voice.volume_state", level=70, mixer="hardware")
        self.bus.push("key.mute_speaker")
        await self.bus.wait("voice.volume")
        self.assertTrue(await self.mind._keys.unmute("a timer is ringing"))
        self.assertEqual(self.bus.of("voice.volume")[-1]["level"], 70)

    async def test_a_keys_brightness_answer_never_answers_a_spoken_brighter(self):
        real = self.bus.send

        async def send(topic, **sent):
            await real(topic, **sent)
            if topic == "being.brightness" and "req" not in sent:
                # A key pressed at the same moment: its answer comes first.
                self.bus.push("body.brightness", ok=True, level=5, at_max=False, at_min=True, limited=True,
                              req=keys.REQ)
                self.bus.push("body.brightness", ok=True, level=100, at_max=True, at_min=False, limited=True)
        self.bus.send = send
        await self.hear("brighter", 1)
        self.assertIn("as bright as I go", " ".join(e["text"] for e in self.bus.of("say.request") if e["text"]))

    async def test_launchpad_opens_the_terminal_without_a_word(self):
        Hands(self.bus)
        self.bus.push("organ.up", name="hands")
        await asyncio.sleep(0)
        self.bus.push("key.apps")
        opened = await self.bus.wait("app.open")
        self.assertEqual(opened["app"], "terminal")
        for _ in range(100):
            if self.mind.apps.view["terminal"] == "shown":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.mind.apps.view["terminal"], "shown")
        self.bus.push("key.apps")
        await self.bus.wait("app.minimise")
        self.assertEqual(self.bus.of("say.request"), [])


if __name__ == "__main__":
    unittest.main()

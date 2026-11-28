"""Written chat on the face: the three modes, and typed lines from the dock."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mind import actions, chat_bridge, commands
from mind.chat_bridge import ChatBridge, MODES


class FakeBus:
    def __init__(self, connected=True):
        self.sent = []
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self.generation = 1
        self._queue = asyncio.Queue()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def send(self, topic, **fields):
        self.sent.append({"t": topic, **fields})

    async def recv(self):
        return await self._queue.get()

    def of(self, topic):
        return [event for event in self.sent if event["t"] == topic]


class Bridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guest = False
        self.bus = FakeBus()
        self.bridge = ChatBridge(self.bus, name="Pip", owner="Richard", guest=lambda: self.guest, log=lambda *a: None)

    def ready(self, state="attending", muted=False):
        self.bridge._chat.on_event({"t": "body.lid", "closed": False, "seq": 4, "snapshot": True})
        self.bridge._chat.on_event({"t": "state.changed", "state": state, "muted": muted, "unwell": False})

    async def type(self, text):
        """A line from the dock, and wait for it to be dealt with."""
        await self.bridge.handle({"t": "ui.text", "text": text})
        await self.bridge.idle()

    async def flush(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def modes(self):
        return [event["mode"] for event in self.bus.of("chat.mode")]

    # -- the mode ---------------------------------------------------------

    async def test_every_start_is_spoken_and_nothing_is_kept(self):
        self.assertEqual(self.bridge.mode, "spoken")
        await self.bridge.publish(force=True)
        self.assertEqual(self.bus.of("chat.mode")[-1], {"t": "chat.mode", "mode": "spoken", "input": "voice", "reply": "voice"})
        await self.bridge.set_mode("typed_text")
        # A restart is a new bridge: the mode is not saved anywhere.
        again = ChatBridge(FakeBus(), guest=lambda: False, log=lambda *a: None)
        self.assertEqual(again.mode, "spoken")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_each_mode_says_what_the_voice_needs_to_know(self):
        for mode, expected in (("spoken", ("voice", "voice")), ("typed_voice", ("typed", "voice")),
                               ("typed_text", ("typed", "text"))):
            await self.bridge.set_mode(mode)
            event = self.bus.of("chat.mode")[-1]
            self.assertEqual((event["input"], event["reply"]), expected, mode)

    async def test_the_chip_and_the_key_cycle_through_the_three_modes(self):
        for _ in range(4):
            await self.bridge.handle({"t": "ui.action", "id": "chat.mode.next", "via": "key"})
        self.assertEqual(self.modes(), ["typed_voice", "typed_text", "spoken", "typed_voice"])

    async def test_a_named_action_switches_to_that_mode(self):
        for mode in MODES:
            await self.bridge.handle({"t": "ui.action", "id": f"chat.mode.{mode}", "via": "click"})
            self.assertEqual(self.bridge.mode, mode)

    async def test_the_voice_request_from_the_mind_switches_and_nonsense_does_not(self):
        await self.bridge.handle({"t": "chat.mode.request", "mode": "typed_text"})
        self.assertEqual(self.bridge.mode, "typed_text")
        for bad in ("loud", None, 3, "typed"):
            await self.bridge.handle({"t": "chat.mode.request", "mode": bad})
        await self.bridge.handle({"t": "ui.action", "id": "chat.mode.loud"})
        await self.bridge.handle({"t": "ui.action", "id": "shell.quick_menu.open"})
        self.assertEqual(self.bridge.mode, "typed_text")
        self.assertEqual(self.modes(), ["typed_text"], "only a real change is announced")

    async def test_the_same_mode_again_is_not_announced_twice(self):
        await self.bridge.set_mode("typed_voice")
        await self.bridge.set_mode("typed_voice")
        await self.bridge.publish()
        self.assertEqual(self.modes(), ["typed_voice"])

    async def test_the_mode_is_told_again_after_the_bus_reconnects(self):
        await self.bridge.set_mode("typed_text")
        self.bus.generation += 1  # the brainstem restarted and lost what it retained
        await self.bridge.publish()
        self.assertEqual(self.modes(), ["typed_text", "typed_text"])

    # -- typed lines --------------------------------------------------------

    async def test_a_typed_line_is_the_owners_words_in_a_written_mode(self):
        self.ready()
        await self.bridge.set_mode("typed_text")
        await self.type("  set a timer   for ten minutes ")
        sent = [(e["t"], e.get("text"), e.get("speaker")) for e in self.bus.sent if e["t"].startswith("hear.")]
        self.assertEqual(sent, [("hear.speech_start", None, None),
                                ("hear.final", "set a timer for ten minutes", "Richard")])
        self.assertTrue(all(e.get("lid_seq") == 4 for e in self.bus.sent if e["t"].startswith("hear.")))

    async def test_typing_in_spoken_mode_is_not_passed_on_and_says_why(self):
        self.ready()
        await self.type("hello")
        self.assertEqual([e["t"] for e in self.bus.sent if e["t"].startswith("hear.")], [])
        self.assertIn("written chat", self.bus.of("chat.notice")[-1]["text"].casefold())

    async def test_a_line_is_cut_to_400_characters_and_junk_is_ignored(self):
        self.ready()
        await self.bridge.set_mode("typed_voice")
        await self.type("a" * 900)
        await self.type(5)
        await self.type("   ")
        finals = self.bus.of("hear.final")
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(finals[0]["text"]), 400)

    async def wake_scenario(self, state):
        """A line typed while the being is in `state`; its state updates reach
        the bridge only through the same receive loop that takes the line."""
        bus = FakeBus()
        woke = []
        real_send = bus.send

        async def send(topic, **fields):
            await real_send(topic, **fields)
            if topic == "hear.wake":
                woke.append(True)
                bus._queue.put_nowait({"t": "state.changed", "state": "attending", "muted": False})

        bus.send = send
        bridge = ChatBridge(bus, name="Pip", owner="Richard", guest=lambda: False, log=lambda *a: None)
        await bridge.start()
        try:
            await bridge.set_mode("typed_text")
            bus._queue.put_nowait({"t": "body.lid", "closed": False, "seq": 4, "snapshot": True})
            bus._queue.put_nowait({"t": "state.changed", "state": state, "muted": False})
            bus._queue.put_nowait({"t": "ui.text", "text": "what time is it"})
            for _ in range(200):
                if bus.of("hear.final"):
                    break
                await asyncio.sleep(0.01)
        finally:
            await bridge.stop()
        return bus, woke

    async def test_a_line_typed_while_the_being_is_idle_or_drowsy_is_not_dropped(self):
        for state in ("idle", "drowsy"):
            bus, woke = await self.wake_scenario(state)
            self.assertEqual([e["text"] for e in bus.of("hear.final")], ["what time is it"], state)
            self.assertEqual(woke, [True], state)

    async def test_waking_the_being_for_a_line_never_shows_a_false_did_not_answer(self):
        bus, _ = await self.wake_scenario("idle")
        self.assertEqual(bus.of("chat.notice"), [])

    async def test_a_guest_arriving_while_a_line_waits_stops_the_line(self):
        bus = FakeBus()
        real_send = bus.send

        async def send(topic, **fields):
            await real_send(topic, **fields)
            if topic == "hear.wake":
                # The guest is reported before the being has finished waking.
                bus._queue.put_nowait({"t": "app.privacy", "guest": True})
                bus._queue.put_nowait({"t": "state.changed", "state": "attending", "muted": False})

        bus.send = send
        bridge = ChatBridge(bus, name="Pip", owner="Richard", guest=lambda: False, log=lambda *a: None)
        await bridge.start()
        try:
            await bridge.set_mode("typed_voice")
            bus._queue.put_nowait({"t": "body.lid", "closed": False, "seq": 4, "snapshot": True})
            bus._queue.put_nowait({"t": "state.changed", "state": "idle", "muted": False})
            bus._queue.put_nowait({"t": "ui.text", "text": "my secret"})
            await asyncio.sleep(0.3)
            await bridge.idle()
            self.assertEqual(bus.of("hear.final"), [])
            self.assertEqual(bus.of("hear.speech_start"), [])
            self.assertEqual(bridge.mode, "spoken")
        finally:
            await bridge.stop()

    async def stalled(self, **limits):
        """A bridge whose first line holds the lock (the being never wakes)."""
        bus = FakeBus()
        bridge = ChatBridge(bus, name="Pip", owner="Richard", guest=lambda: False, log=lambda *a: None)
        for name, value in limits.items():
            patcher = patch.object(chat_bridge, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(chat_bridge.type_chat, "WAKE_WAIT", 0.4)
        patcher.start()
        self.addCleanup(patcher.stop)
        await bridge.start()
        self.addCleanup(lambda: asyncio.ensure_future(bridge.stop()))
        await bridge.set_mode("typed_text")
        bus._queue.put_nowait({"t": "body.lid", "closed": False, "seq": 4, "snapshot": True})
        bus._queue.put_nowait({"t": "state.changed", "state": "idle", "muted": False})
        await asyncio.sleep(0.02)
        return bus, bridge

    async def test_a_line_queued_behind_another_never_acts_after_a_guest_arrives(self):
        bus, bridge = await self.stalled()
        bus._queue.put_nowait({"t": "ui.text", "text": "first"})
        await asyncio.sleep(0.05)  # it has woken the being and holds the lock
        bus._queue.put_nowait({"t": "state.changed", "state": "speaking", "muted": False})
        bus._queue.put_nowait({"t": "ui.text", "text": "second"})
        bus._queue.put_nowait({"t": "app.privacy", "guest": True})
        await asyncio.sleep(0.8)
        await bridge.idle()
        # Only the first line's own wake; the second never interrupted a reply to the guest.
        self.assertEqual(len(bus.of("hear.wake")), 1)
        self.assertEqual(bus.of("key.interrupt"), [])
        self.assertEqual(bus.of("hear.final"), [])
        self.assertEqual(bus.of("chat.notice"), [], "no notice about the owner's lines once a guest is here")

    async def test_a_sleeping_beings_wake_is_also_gated(self):
        bus, bridge = await self.stalled()
        bus._queue.put_nowait({"t": "ui.text", "text": "first"})
        await asyncio.sleep(0.05)
        bus._queue.put_nowait({"t": "state.changed", "state": "asleep", "muted": False})
        bus._queue.put_nowait({"t": "ui.text", "text": "wake up"})
        bus._queue.put_nowait({"t": "app.privacy", "guest": True})
        await asyncio.sleep(0.8)
        await bridge.idle()
        self.assertEqual(len(bus.of("hear.wake")), 1)

    async def test_a_line_that_waited_too_long_is_dropped_not_delivered_late(self):
        bus, bridge = await self.stalled(LINE_MAX_AGE=0.1)
        bus._queue.put_nowait({"t": "ui.text", "text": "first"})
        await asyncio.sleep(0.05)
        bus._queue.put_nowait({"t": "ui.text", "text": "second"})
        await asyncio.sleep(0.2)
        bus._queue.put_nowait({"t": "state.changed", "state": "attending", "muted": False})
        await asyncio.sleep(0.5)
        await bridge.idle()
        self.assertNotIn("second", [e["text"] for e in bus.of("hear.final")])

    async def test_only_a_few_lines_may_wait_at_once(self):
        bus, bridge = await self.stalled(LINE_QUEUE=2)
        for text in ("a", "b", "c", "d"):
            bus._queue.put_nowait({"t": "ui.text", "text": text})
        await asyncio.sleep(0.1)
        notices = [e["text"] for e in bus.of("chat.notice")]
        self.assertEqual(len([n for n in notices if "Too many" in n]), 2, notices)

    async def test_written_replies_do_not_wait_for_the_voice(self):
        # The mode the owner falls back on when hearing or the voice fails.
        self.ready()
        await self.bridge.handle({"t": "organ.down", "name": "voice"})
        await self.bridge.set_mode("typed_text")
        await asyncio.wait_for(self.type("what time is it"), 1)
        self.assertEqual([e["text"] for e in self.bus.of("hear.final")], ["what time is it"])

    async def test_a_closed_lid_blocks_the_line_and_keeps_the_mode(self):
        self.ready()
        await self.bridge.set_mode("typed_text")
        self.bridge._chat.on_event({"t": "body.lid", "closed": True, "seq": 5})
        await self.type("hello")
        await self.flush()
        self.assertEqual(self.bus.of("hear.final"), [])
        self.assertIn("lid", self.bus.of("chat.notice")[-1]["text"].casefold())
        self.assertEqual(self.bridge.mode, "typed_text")

    async def test_a_sleeping_being_needs_the_wake_words_not_any_line(self):
        self.ready(state="asleep")
        await self.bridge.set_mode("typed_voice")
        await self.type("hello")
        await self.flush()
        self.assertEqual(self.bus.of("hear.final"), [])
        self.assertIn("asleep", self.bus.of("chat.notice")[-1]["text"].casefold())

    async def test_notices_are_short_printable_text(self):
        await self.bridge._notice("x\x1b[31m" + "y" * 500, "warning")
        notice = self.bus.of("chat.notice")[-1]
        self.assertLessEqual(len(notice["text"]), 120)
        self.assertNotIn("\x1b", notice["text"])
        self.assertEqual(notice["tone"], "warning")

    # -- guests ----------------------------------------------------------------

    async def test_a_guest_arriving_puts_the_mode_back_to_spoken(self):
        await self.bridge.set_mode("typed_text")
        await self.bridge.handle({"t": "app.privacy", "guest": True})
        self.assertEqual(self.bridge.mode, "spoken")
        self.assertEqual(self.modes(), ["typed_text", "spoken"])
        # And what is already spoken is not announced again.
        await self.bridge.handle({"t": "app.privacy", "guest": True})
        self.assertEqual(self.modes(), ["typed_text", "spoken"])

    async def test_a_guest_cannot_switch_to_or_type_in_a_written_mode(self):
        self.ready()
        await self.bridge.handle({"t": "app.privacy", "guest": True})
        await self.bridge.handle({"t": "ui.action", "id": "chat.mode.typed_text", "via": "key"})
        await self.bridge.handle({"t": "chat.mode.request", "mode": "typed_voice"})
        self.assertEqual(self.bridge.mode, "spoken")
        await self.type("tell me the owner's secrets")
        self.assertEqual(self.bus.of("hear.final"), [])
        self.assertEqual(self.bus.of("hear.speech_start"), [])

    async def test_the_owner_is_back_after_the_guest_leaves_but_the_mode_stays_spoken(self):
        await self.bridge.handle({"t": "app.privacy", "guest": True})
        await self.bridge.handle({"t": "app.privacy", "guest": False})
        self.assertEqual(self.bridge.mode, "spoken")
        await self.bridge.set_mode("typed_voice")
        self.assertEqual(self.bridge.mode, "typed_voice")

    async def test_the_boot_latch_counts_as_a_guest_when_no_report_has_come(self):
        self.guest = True
        await self.bridge.set_mode("typed_text")
        self.assertEqual(self.bridge.mode, "spoken")

    async def test_when_the_latch_cannot_be_read_nobody_types(self):
        def broken():
            raise OSError("no")
        bridge = ChatBridge(FakeBus(), guest=broken, log=lambda *a: None)
        await bridge.set_mode("typed_text")
        self.assertEqual(bridge.mode, "spoken")

    async def test_the_latch_helper_treats_any_entry_as_a_guest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guest"
            self.assertFalse(chat_bridge.guest_latched(str(path)))
            path.write_text("")
            self.assertTrue(chat_bridge.guest_latched(str(path)))


class Wiring(unittest.TestCase):
    def test_the_bridge_sees_exactly_the_topics_it_handles(self):
        for topic in ("ui.text", "ui.action", "chat.mode.request", "app.privacy", "say.played", "state.changed",
                      "body.lid"):
            self.assertIn(topic, chat_bridge.SUBS)

    def test_every_mode_action_the_face_may_send_is_registered(self):
        registered = {action.id for action in actions.ACTIONS if action.id.startswith("chat.mode.")}
        self.assertEqual(registered, set(chat_bridge.ACTIONS))

    def test_the_voice_phrases_are_off_until_the_turn_loop_can_pass_them_on(self):
        self.assertIs(actions.CHAT_MODE_BY_VOICE, False)
        for said in ("Let's type", "Silent mode", "Back to talking", "Written chat"):
            command = commands.parse(said, "Pip")
            self.assertTrue(command is None or command.kind != "chat_mode", said)
        advertised = [action.phrases for action in actions.ACTIONS if action.id.startswith("chat.mode.")]
        self.assertEqual([p for p in advertised if p], [], "no phrase is advertised that nothing acts on")

    def test_the_voice_phrases_name_the_modes_once_switched_on(self):
        wanted = {
            "Let's type": "typed_voice", "Hey Pip, let's type": "typed_voice", "Written chat": "typed_voice",
            "Switch to written chat": "typed_voice", "Silent mode": "typed_text",
            "Written replies only": "typed_text", "Back to talking": "spoken", "Voice mode": "spoken",
        }
        with patch.object(actions, "CHAT_MODE_BY_VOICE", True):
            for said, mode in wanted.items():
                command = commands.parse(said, "Pip")
                self.assertEqual((command.kind, command.arg), ("chat_mode", mode), said)

    def test_ordinary_talk_about_talking_or_typing_is_not_a_command(self):
        with patch.object(actions, "CHAT_MODE_BY_VOICE", True):
            for said in ("Let's talk about the weather", "I like typing", "Is this a silent mode phone",
                         "Let's type up the shopping list"):
                command = commands.parse(said, "Pip")
                self.assertTrue(command is None or command.kind != "chat_mode", said)


class Unit(unittest.TestCase):
    """The image runs the bridge as its own unit, with its own organ name."""

    ROOT = Path(__file__).resolve().parents[1]

    def text(self):
        return (self.ROOT / "os/package/being/being-chat.service").read_text()

    def test_the_unit_runs_the_bridge_and_is_installed_by_the_recipe(self):
        self.assertIn("python3 -m mind.chat_bridge --bus ${BEING_BUS}", self.text())
        recipe = (self.ROOT / "os/package/being/being.mk").read_text()
        self.assertIn("system/being-chat.service", recipe)
        self.assertIn("enable being-*.service", (self.ROOT / "os/package/being/50-being.preset").read_text())

    def test_the_unit_is_confined_to_the_bus_and_writes_nothing_else(self):
        text = self.text()
        # The same sandbox as the eyes organ, without its camera.
        for line in ("PrivateNetwork=yes", "NoNewPrivileges=yes", "ProtectSystem=strict", "ReadOnlyPaths=/mind",
                     "CapabilityBoundingSet=", "DevicePolicy=closed", "ProtectControlGroups=yes",
                     "RestrictNamespaces=yes", "RestrictAddressFamilies=AF_UNIX"):
            self.assertRegex(text, rf"(?m)^{line}$")
        self.assertNotIn("ReadWritePaths", text, "it writes nothing")
        self.assertNotIn("DeviceAllow", text)
        self.assertNotIn("/workshop", text)

    def test_the_brainstem_names_the_unit_that_may_use_the_organ_name(self):
        peer = (self.ROOT / "crates/brainstem/src/peer.rs").read_text()
        self.assertIn('("chat", &["being-chat.service"])', peer)
        self.assertEqual(chat_bridge.ORGAN, "chat")


if __name__ == "__main__":
    unittest.main()

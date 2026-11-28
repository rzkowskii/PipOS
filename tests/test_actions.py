"""Check the advertised examples against the real code and workshop routes."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mind import actions, app_commands, calc, commands, device, look, setup, turn
from mind.profile import Profile
from tests.test_mind import MindCase, said
from tests.test_turn_apps import Hands


class ActionRegistryTests(unittest.TestCase):
    def test_each_canonical_phrase_reaches_its_own_code(self):
        self.assertEqual(set(actions.LOOK_FIELDS), set(look.FIELDS))
        self.assertEqual(len({action.id for action in actions.ACTIONS}), len(actions.ACTIONS))
        for action in actions.ACTIONS:
            for phrase in action.phrases:
                utterance = f"Hey Robin, {phrase}."
                with self.subTest(id=action.id, phrase=phrase):
                    if action.id.startswith("app."):
                        parts = action.id.split(".")
                        target, kind = ("calc", "close") if len(parts) == 2 else parts[1:]
                        view = {"restore": "minimised", "open": "closed"}.get(kind, "shown")
                        command = app_commands.parse(utterance, app_commands.AppContext(**{target: view}), "Robin")
                        self.assertEqual((command.app, command.kind), (target, kind))
                    elif action.id == "calc.sum":
                        self.assertIn("forty-two", calc.answer(utterance, "Robin"))
                    elif action.id == "keys.brightness":
                        self.assertIsNotNone(device.classify(phrase))
                    else:
                        result = commands.parse(utterance, "Robin")
                        self.assertIsNotNone(result)
                        if action.id == "look.current":
                            self.assertEqual(result.kind, "look_current")
                        elif action.id == "keys.volume":
                            self.assertIn(result.kind, ("volume_up", "volume_down"))
                        elif action.id.startswith("chat.mode."):
                            self.assertEqual((result.kind, result.arg), ("chat_mode", action.id.removeprefix("chat.mode.")))
                        elif action.id.startswith("shell.quick_menu."):
                            self.assertEqual(result.kind, "quick_menu")
                            self.assertIn(result.arg, ("open", "close", "left", "center", "right"))
                        else:
                            self.assertEqual(result.kind, "look_set")
                            key, heard = result.arg.split(" ", 1)
                            self.assertEqual(key, action.id.removeprefix("look."))
                            self.assertTrue(look.valid(key, look.spoken_value(key, heard, look.Look(),
                                                                           commands.spoken_number)))
        self.assertTrue(set(actions.app_phrases()) <= set(app_commands.grammar_phrases()))

    def test_installable_setup_page_uses_the_registry_examples(self):
        with (tempfile.TemporaryDirectory() as temp,
              patch("builtins.input", side_effect=["v", "1", "", "3", "", "4", "", "0", "q"]),
              patch.object(setup, "available_voices", return_value=set()),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertIsNone(setup.personalize(Profile(), Path(temp)))
            self.assertEqual(list(Path(temp).iterdir()), [])
        displayed = setup.ui.plain(output.getvalue())
        for text in ("Make your eyes cyan", "Hide your mouth", "Open the calculator",
                     "What is six times seven", "fn+F1", "fn+F10"):
            self.assertIn(text, displayed)

    def test_help_topics_do_not_claim_unrelated_speech(self):
        self.assertEqual(commands.parse("Help with reminders.").kind, "skills_group")
        self.assertEqual(commands.parse("What can I say about screen apps?").arg, "surfaces")
        for phrase in ("Help with my homework", "I wonder what I can say about appearance",
                       "What can I say about my new bike?", "Show me the voice commands from my book"):
            self.assertIsNone(commands.parse(phrase), phrase)

    def test_help_reports_an_unavailable_panel_and_suggests_arithmetic(self):
        self.assertIn("aren't available", actions.spoken_help("surfaces", panels=False))
        self.assertIn("six times seven", actions.spoken_help("calculator", panels=False))
        self.assertNotIn("Open the calculator", actions.spoken_help("calculator", panels=False))

    def test_menu_positions_are_explicit_commands_with_near_misses(self):
        for phrase, position in (("Put the menu on the left", "left"), ("Move the menu right", "right"),
                                 ("Move the Quick Menu to the centre", "center")):
            self.assertEqual(commands.parse(phrase), commands.Command("quick_menu", position))
        for phrase in ("What's left on the menu", "Move the app to the left", "Move it left",
                       "Can I make a quick menu for lunch", "Open a photo of the Quick Menu"):
            self.assertIsNone(commands.parse(phrase), phrase)

    def test_menu_correction_is_exact_and_absent_from_global_commands(self):
        for phrase, position in (("Actually, put it on the left.", "left"),
                                 ("Hey Robin, actually put it on the RIGHT", "right"),
                                 ("Actually, put it on the centre", "center")):
            self.assertIsNone(commands.parse(phrase, "Robin"))
            self.assertEqual(commands.quick_menu_followup(phrase, "Robin"), position)
        for phrase in ("Put it on the left", "Actually put it left", "Actually put my app on the left",
                       "Actually, put it on the left tomorrow", "Actually, leave it on the left"):
            self.assertIsNone(commands.quick_menu_followup(phrase, "Robin"), phrase)


class QuickMenuVoice(MindCase):
    async def follow(self, text, number, *, speaker="Richard"):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text=text, speaker=speaker, followup=True)
        reply = await self.bus.wait("mind.reply_done", number)
        self.bus.push("say.done", final=True)
        return reply["spoke"]

    def positions(self):
        return [event["position"] for event in self.bus.of("screen.quick_menu") if "position" in event]

    async def test_spoken_menu_visibility_is_ephemeral_and_placement_is_saved(self):
        await self.hear("Hey Pip, open the Quick Menu.", 1)
        await self.hear("Move the quick menu left.", 2)
        await self.hear("Close the Quick Menu.", 3)
        wanted = [{"open": True}, {"position": "left"}, {"open": False}]
        events = [event for event in self.bus.sent if event["t"] == "screen.quick_menu"]
        self.assertEqual([{key: value for key, value in event.items() if key not in {"t", "lid_seq"}} for event in events],
                         wanted)
        await self.hear("Start guest mode.", 4)
        await self.hear("Open the Quick Menu.", 5)
        self.assertEqual(len([event for event in self.bus.sent if event["t"] == "screen.quick_menu"]), 3)
        self.assertIn("closed in guest mode", said(self.bus))
        self.assertEqual(look.Store(self.root).read().menu_position, "left")

    async def test_options_keys_require_a_current_lid_epoch_and_owner_session(self):
        self.bus.push("body.lid", closed=False, seq=11)
        await self.hear("Open Options.", 1)
        opened = self.bus.of("screen.quick_menu")[-1]
        self.assertIs(opened["options"], True)
        self.assertIn("Appearance|Make your eyes blue", opened["options_help"])

        def edit(seq, field, value):
            self.bus.push("ui.action", id="shell.options.set", via="key", lid_seq=seq, field=field, value=value)

        edit(10, "captions", True)
        edit(11, "clock_widget_position", "bottom-left")
        await self.bus.wait("screen.prefs", len(self.bus.of("screen.prefs")) + 1)
        saved = look.Store(self.root).read()
        self.assertFalse(saved.captions)
        self.assertEqual(saved.clock_widget_position, "bottom-left")

        await self.hear("Start guest mode.", 2)
        edit(11, "captions", True)
        await self.hear("Close Options.", 3)
        self.assertEqual(look.Store(self.root).read(), saved)

    async def test_a_spoken_app_takes_over_from_the_visible_menu(self):
        hands = Hands(self.bus)
        self.bus.push("organ.up", name="hands")
        await self.hear("Open the Quick Menu.", 1)
        await self.hear("Open the calculator.", 2)
        self.assertEqual(hands.requests[-1]["app"], "calc")
        self.assertEqual([e["open"] for e in self.bus.of("screen.quick_menu")], [True, False])
        self.assertFalse(await self.follow("Actually, put it on the right", 3))
        self.assertEqual(self.positions(), [])

    async def test_recent_owner_context_reuses_the_handler_without_renewing_its_deadline(self):
        with patch("mind.turn._uptime", return_value=1000.) as uptime:
            self.assertFalse(await self.follow("Actually, put it on the left", 1))
            self.assertEqual(self.positions(), [])
            await self.hear("Open the Quick Menu", 2)
            self.assertFalse(await self.follow("Actually, put it on the right", 3, speaker="Visitor"))
            uptime.return_value += turn.QUICK_MENU_CONTEXT_SECONDS - 1
            self.assertTrue(await self.follow("Actually, put it on the centre", 4))
            self.assertEqual(self.positions(), ["center"])
            uptime.return_value += 2
            self.assertFalse(await self.follow("Actually, put it on the left", 5))
            self.assertEqual(self.positions(), ["center"])

            await self.hear("Move the Quick Menu to the right", 6)
            self.assertTrue(await self.follow("Actually put it on the left", 7))
            self.assertEqual(self.positions(), ["center", "right", "left"])

    async def test_another_speaker_cannot_establish_a_context_for_the_owner(self):
        self.bus.push("state.changed", state="attending")
        self.bus.push("hear.final", text="Open the Quick Menu", speaker="Visitor")
        await self.bus.wait("mind.reply_done")
        self.bus.push("say.done", final=True)
        self.assertFalse(await self.follow("Actually, put it on the right", 2))
        self.assertEqual(self.positions(), [])

    async def test_another_addressed_task_ends_context_and_addressed_fallback_still_routes(self):
        await self.hear("Open the Quick Menu", 1)
        await self.hear("What time is it?", 2)
        self.assertFalse(await self.follow("Actually, put it on the right", 3))
        await self.hear("Pip, actually, put it on the left", 4)
        self.assertEqual(self.positions(), [])
        self.assertEqual(self.bus.of("mind.route")[-1]["via"], "model")

    async def test_explicit_dismiss_keyboard_dismiss_and_face_loss_end_context(self):
        self.bus.push("body.lid", closed=True, seq=10)
        self.bus.push("body.lid", closed=False, seq=11)
        await self.hear("Open the Quick Menu", 1)
        self.bus.push("ui.action", id="shell.quick_menu.dismissed", via="key", lid_seq=9)
        self.assertTrue(await self.follow("Actually, put it on the right", 2))
        self.bus.push("ui.action", id="shell.quick_menu.dismissed", via="key", lid_seq=11)
        self.assertFalse(await self.follow("Actually, put it on the left", 3))
        await self.hear("Open the Quick Menu", 4)
        self.bus.push("organ.down", name="face")
        self.assertFalse(await self.follow("Actually, put it on the left", 5))
        await self.hear("Open the Quick Menu", 6)
        await self.hear("Close the Quick Menu", 7)
        self.assertFalse(await self.follow("Actually, put it on the left", 8))
        self.assertEqual(self.positions(), ["right"])

    async def test_dismiss_during_routing_stops_a_previously_resolved_move(self):
        self.bus.push("body.lid", closed=False, seq=11)
        await self.hear("Open the Quick Menu", 1)
        send = self.bus.send

        async def dismiss_at_route(topic, **fields):
            if topic == "mind.route":
                self.bus.push("ui.action", id="shell.quick_menu.dismissed", via="key", lid_seq=11)
            await send(topic, **fields)

        with patch.object(self.bus, "send", dismiss_at_route):
            self.assertFalse(await self.follow("Actually, put it on the right", 2))
        self.assertEqual(self.positions(), [])

    async def test_lid_disconnect_and_guest_clear_the_owners_context(self):
        await self.hear("Open the Quick Menu", 1)
        self.bus.push("body.lid", closed=True, seq=1)
        self.bus.push("body.lid", closed=False, seq=2)
        await self.hear("Pip, actually, put it on the right", 2)
        await self.hear("Open the Quick Menu", 3)
        self.bus.connected.clear()
        self.bus.on_disconnect()
        self.bus.connected.set()
        self.bus.push("body.lid", closed=False, seq=3)
        await self.hear("Pip, actually, put it on the right", 4)
        await self.hear("Open the Quick Menu", 5)
        await self.hear("Start guest mode", 6)
        self.assertFalse(await self.follow("Actually, put it on the right", 7))
        await self.hear("End guest mode", 8)
        self.assertFalse(await self.follow("Actually, put it on the right", 9))
        self.assertEqual(self.positions(), [])

    async def test_unrelated_words_keep_a_strict_pending_question_or_existing_forget_precedence(self):
        await self.hear("Open the Quick Menu", 1)
        on_yes = AsyncMock()
        pending = {"speaker": "Richard", "until": turn._uptime() + 30,
                   "confirm": SimpleNamespace(strict=True, on_yes=on_yes)}
        self.mind._pending_confirm = pending
        self.assertFalse(await self.follow("Actually, put it on the left", 2))
        self.assertIs(self.mind._pending_confirm, pending)
        await self.hear("Pip, actually, put it on the left", 3)
        self.assertIsNone(self.mind._pending_confirm)
        on_yes.assert_not_awaited()

        await self.hear("Open the Quick Menu", 4)
        self.mind._pending_forget = {"speaker": "Richard", "until": turn._uptime() + 30}
        await self.hear("Pip, actually, put it on the right", 5)
        self.assertIn("kept everything", said(self.bus))
        self.assertEqual(self.positions(), [])

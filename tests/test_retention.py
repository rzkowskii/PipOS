"""Retention and guest boundaries exercised through addressed turns and restarts."""

import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

from mind import care_routes, continuity, core_profile
from mind.cortex import SLOT_FILE
from mind.initiative import InitiativePolicy, QuietHours, save_policy as save_initiative
from mind.retention import POLICY_FILE, MemoryPolicy, save_policy
from tests.test_mind import MindCase, said
from tests.disk_roomy import setUpModule  # noqa: F401  (disk guard is not under test here)


DAY = 24 * 60 * 60
OWNER_HISTORY = "I keep the blue atlas named Atlaswick on my bookshelf"
OWNER_FACT = "My orchid is called Orchidmarker"
OWNER_CORE = "OwnerCoreMarker"
GUEST_HISTORY = "This amber kite is named Kiteguest"
GUEST_FACT = "My guest badge is called Badgeguest"


class RetentionCase(MindCase):
    async def say(self, text):
        self.bus.sent.clear()
        await self.hear(text, 1)
        reply = said(self.bus)
        self.assertTrue(reply, f"No spoken reply to {text!r}")
        return reply

    async def restart(self, policy=None):
        await self.stop_mind()
        if policy is not None:
            save_policy(self.root, policy)
        await self.start_mind()

    async def seed_owner(self):
        await self.say(OWNER_HISTORY)
        await self.say(f"Remember that {OWNER_FACT}")

    def latest_prompt(self):
        return json.dumps(self.cortex_server.chats[-1]["messages"])

    def assert_not_persisted(self, *needles):
        for path in self.root.rglob("*"):
            if path.is_file():
                contents = path.read_bytes()
                for needle in needles:
                    self.assertNotIn(needle.encode(), contents, f"{needle!r} reached {path.name}")


class ExplicitOnlyForgetting(RetentionCase):
    async def test_forget_that_drops_the_last_live_exchange_when_nothing_is_archived(self):
        await self.restart(MemoryPolicy(mode="explicit-only"))
        await self.say("My neighbour is called Quillonmarker")
        self.assertIn("Quillonmarker", json.dumps(self.mind.messages))
        self.assertEqual(await self.say("Forget that"), "Okay. That's forgotten.")
        self.assertNotIn("Quillonmarker", json.dumps(self.mind.messages))
        self.assert_not_persisted("Quillonmarker")


class RetentionConversation(RetentionCase):
    async def test_owner_can_resume_a_saved_thread_after_restart_and_then_close_it(self):
        source = self.memory.log("heard", "I'm planning a new stained glass window", "Richard")
        self.assertIn("keep that thread open", await self.say("Pick this up later"))
        await self.restart()
        self.assertIn("stained glass window", await self.say("What were we working on?"))
        self.assertIn(source, self.mind.highlights)
        self.assertIn("closed the thread", await self.say("Close the thread"))
        self.assertNotIn(source, self.mind.highlights)
        self.assertIn("isn't an open thread", await self.say("What were we working on?"))

    async def test_due_thread_waits_for_addressed_greeting_outside_quiet_hours(self):
        now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        self.mind.skills.zone = timezone.utc
        self.mind._newborn = False
        end = now.timestamp() - DAY
        with patch.object(self.memory, "now", return_value=end - 2 * DAY):
            self.memory.log("heard", "Tomorrow I have a dentist appointment", "Richard",
                            event_start=end - DAY, event_end=end)
            continuity.bookmark(self.memory, "Richard")
        with (patch.object(self.mind, "_clock_doubt", return_value=None),
              patch.object(self.mind.skills, "now", return_value=now)):
            save_initiative(self.root, InitiativePolicy(greetings=True, quiet_hours=QuietHours("11:00", "13:00")))
            self.assertNotIn("dentist", await self.say("Hi Pip"))
            save_initiative(self.root, InitiativePolicy(greetings=True))

            self.bus.sent.clear()
            self.bus.push("state.changed", state="attending")
            self.bus.push("hear.final", text="Hello", speaker="Richard", followup=True)
            await self.bus.wait("mind.reply_done")
            self.assertNotIn("dentist", said(self.bus))

            answer = await self.say("Hi Pip")
            self.assertIn("dentist appointment", answer)
            self.assertIn("How did it go?", answer)
            self.assertNotIn("dentist", await self.say("Hi Pip"))

    async def test_generated_core_is_a_reply_dependency_and_is_hidden_from_guests(self):
        fact = self.memory.remember(OWNER_FACT, "Richard")
        core_profile.publish(self.memory, "Richard")
        await self.restart()

        await self.say("Would a wooden bookshelf fit in my room?")
        self.assertIn("Orchidmarker", self.latest_prompt())
        episode, meta = self.memory.db.execute(
            "SELECT id, meta FROM episodes WHERE kind = 'said' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIn(fact, json.loads(meta)["recalled"])

        await self.say("Start guest mode")
        self.assertNotIn("Orchidmarker", self.mind.system)
        await self.say("End guest mode")
        self.memory.forget((fact,))
        self.assertNotIn("Orchidmarker", self.mind.system)
        self.assertEqual(self.memory.db.execute("SELECT text FROM episodes WHERE id = ?", (episode,)).fetchone(),
                         ("[forgotten]",))

    async def test_default_persistent_mode_recalls_ordinary_history_after_restart(self):
        await self.seed_owner()

        await self.restart()

        self.assertIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        self.assertIn("Orchidmarker", await self.say("What do you remember about my orchid?"))

    async def test_explicit_only_keeps_explicit_facts_and_masks_ordinary_history(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(mode="explicit-only"))
        slot_boundary = len(self.cortex_server.slot_calls)
        ordinary = "The chalk easel for this chat is called Easelmarker"
        explicit = "My reusable ticket is called Ticketmarker"

        self.assertNotIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        await self.say("Would a wooden bookshelf fit in my room?")
        self.assertNotIn("Atlaswick", self.latest_prompt())
        await self.say(ordinary)
        await self.say(f"Remember that {explicit}")
        await self.restart()

        self.assertIn("Ticketmarker", await self.say("What do you remember about my reusable ticket?"))
        self.assertNotIn("Easelmarker", await self.say("What did I say about the chalk easel?"))
        self.assertFalse(any(action == "action=save" for action, _ in self.cortex_server.slot_calls[slot_boundary:]))
        self.assert_not_persisted("Easelmarker")
        self.assertTrue(self.memory.contains("Atlaswick"), "switching modes must not destroy prior history")
        await self.restart(MemoryPolicy())
        self.assertIn("Atlaswick", await self.say("What did I say about my bookshelf?"))

    async def test_session_only_remembers_within_the_run_then_starts_blank(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(mode="session-only"))
        slot_boundary = len(self.cortex_server.slot_calls)
        ordinary = "The cello in this conversation is called Cellomarker"
        explicit = "My matchbox is called Matchmarker"

        self.assertNotIn("Orchidmarker", await self.say("What do you remember about my orchid?"))
        self.assertNotIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        await self.say(ordinary)
        self.assertIn("Cellomarker", await self.say("What did I say about the cello?"))
        await self.say(f"Remember that {explicit}")
        self.assertIn("Matchmarker", await self.say("What do you remember about my matchbox?"))
        await self.restart()

        self.assertNotIn("Cellomarker", await self.say("What did I say about the cello?"))
        self.assertNotIn("Matchmarker", await self.say("What do you remember about my matchbox?"))
        self.assertTrue(self.memory.contains("Orchidmarker"))
        self.assertFalse(any(action == "action=save" for action, _ in self.cortex_server.slot_calls[slot_boundary:]))
        self.assert_not_persisted("Cellomarker", "Matchmarker")

    async def test_invalid_policy_does_not_reopen_the_owner_archive(self):
        await self.seed_owner()
        await self.stop_mind()
        (self.root / POLICY_FILE).write_text('{"version": 1, "mode":', encoding="utf-8")
        await self.start_mind()

        self.assertNotIn("Orchidmarker", await self.say("What do you remember about my orchid?"))
        self.assertNotIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        self.assertTrue(self.memory.contains("Orchidmarker"))

    async def test_expiry_drops_old_sources_and_new_dependent_replies(self):
        archived = "My brass lantern is called Lanternmarker"
        old_fact = "The cellar padlock is called Lockmarker"
        retained = "My portable radio is called Radiomarker"
        derived = "The cellar hint is written under the first drawer."
        await self.say(archived)
        await self.say(f"Remember that {old_fact}")
        self.cortex_server.reply = lambda _: f"[neutral] {derived}"
        self.assertIn(derived, await self.say("Could we talk about the cellar padlock?"))
        self.assertIn("Lockmarker", self.latest_prompt())
        await self.say(f"Remember that {retained}")
        aging = time.time() - 4 * DAY
        with self.memory.db:
            self.memory.db.execute("UPDATE episodes SET ts = ? WHERE text = ? OR text = ?",
                                   (aging, archived, f"Remember that {old_fact}"))
            self.memory.db.execute("UPDATE facts SET ts = ? WHERE text = ?", (aging, old_fact))

        slot_boundary = len(self.cortex_server.slot_calls)
        await self.restart(MemoryPolicy(expiry_days=2))

        self.assertNotIn("Lanternmarker", await self.say("What did I say about the brass lantern?"))
        self.assertNotIn("Lockmarker", await self.say("What do you remember about the cellar padlock?"))
        self.assertIn("Radiomarker", await self.say("What do you remember about my portable radio?"))
        for text in (archived, old_fact, derived):
            self.assertFalse(self.memory.contains(text), text)
        self.cortex_server.reply = self.reply
        await self.say("Could we talk about keeping a camera lens clean?")
        prompt = self.latest_prompt()
        for text in ("Lanternmarker", "Lockmarker", derived):
            self.assertNotIn(text, prompt)
        self.assertFalse(any(action == "action=restore" for action, _ in self.cortex_server.slot_calls[slot_boundary:]))

    def stamp(self, episode):
        return self.memory.db.execute("SELECT ts FROM episodes WHERE id = ?", (episode,)).fetchone()[0]

    async def check(self, at):
        with patch("mind.turn.time.time", return_value=at):
            self.mind._expiry_day = None
            await self.mind._check_expiry()

    async def test_a_dead_clock_battery_expires_nothing_then_or_after_it_is_set(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(expiry_days=2))
        real = time.time()
        # The laptop wakes in 2001 and is talked to for a while.
        with patch("mind.turn.time.time", return_value=1_000_000.0):
            await self.mind._check_expiry()
            spoken = self.memory.log("heard", "The spare key is under Flowerpotmarker", "Richard")
        # The clock is then set right. Nothing said meanwhile is taken for old.
        await self.check(real + 5)
        self.assertGreater(self.stamp(spoken), real - 60, "what was said under the dead clock is recent")
        await self.check(real + 3 * DAY / 2)
        for text in ("Atlaswick", "Orchidmarker", "Flowerpotmarker"):
            self.assertTrue(self.memory.contains(text), text)

    async def test_a_clock_years_ahead_expires_nothing_until_it_has_held_for_a_week(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(expiry_days=30))
        real = time.time()
        ahead = real + 5 * 365 * DAY
        await self.check(ahead)
        self.assertTrue(self.memory.contains("Atlaswick"))
        self.assertIsNotNone(self.mind._expiry_revision, "the context is still saved meanwhile")
        # One wrong reading, then the right time again: nothing is lost.
        await self.check(real + 5)
        self.assertTrue(self.memory.contains("Atlaswick"))
        # A clock that stays ahead and keeps going for a week was a long time off.
        # What was said during that week is new, and stays.
        await self.check(ahead)
        with patch("mind.turn.time.time", return_value=ahead + 3 * DAY):
            during = self.memory.log("heard", "The new neighbour is called Weekmarker", "Richard")
        await self.check(ahead + 8 * DAY)
        self.assertFalse(self.memory.contains("Atlaswick"))
        self.assertTrue(self.memory.contains("Weekmarker"))
        self.assertEqual(self.stamp(during), ahead + 3 * DAY)

    async def test_a_date_typed_a_year_ahead_and_put_right_expires_nothing(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(expiry_days=30))
        real = time.time()
        await self.check(real + 365 * DAY)
        self.assertTrue(self.memory.contains("Atlaswick"), "a step forward is not acted on at once")
        with patch("mind.turn.time.time", return_value=real + 365 * DAY + 60):
            wrong = self.memory.log("heard", "The plumber is called Typomarker", "Richard")
        await self.check(real + 120)
        await self.check(real + 2 * DAY)
        for text in ("Atlaswick", "Orchidmarker", "Typomarker"):
            self.assertTrue(self.memory.contains(text), text)
        self.assertLess(self.stamp(wrong), real + DAY, "its stamp was put right too")

    async def test_three_weeks_switched_off_still_expires_a_day_after_coming_back(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(expiry_days=10))
        real = time.time()
        await self.check(real + 21 * DAY)
        self.assertTrue(self.memory.contains("Atlaswick"))
        await self.check(real + 22 * DAY)
        self.assertTrue(self.memory.contains("Atlaswick"), "three weeks off has to hold for a week")
        await self.check(real + 28 * DAY + 60)
        self.assertFalse(self.memory.contains("Atlaswick"))

    async def test_a_dead_battery_reboot_while_a_step_is_unsettled_loses_nothing(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(expiry_days=2))
        real = time.time()
        await self.check(real + 2 * DAY)  # two days off, clock set: unsettled
        with patch("mind.turn.time.time", return_value=1_000_000.0):  # reboots in 2001
            spoken = self.memory.log("heard", "The meter cupboard key is Metermarker", "Richard")
        await self.check(real + 2 * DAY + 3600)  # set again
        await self.check(real + 3 * DAY + 7200)
        self.assertTrue(self.memory.contains("Metermarker"))
        self.assertGreater(self.stamp(spoken), real + DAY)

    async def test_a_clock_a_month_back_for_ten_days_then_put_right_loses_nothing_recent(self):
        await self.seed_owner()
        await self.restart(MemoryPolicy(expiry_days=30))
        real = time.time()
        back = real - 30 * DAY
        await self.check(back + 60)
        for day in range(1, 11):
            with patch("mind.turn.time.time", return_value=back + day * DAY):
                self.memory.log("heard", f"Day {day} I planted Seedmarker{day}", "Richard")
            await self.check(back + day * DAY + 60)
        await self.check(real + 10 * DAY)  # put right
        await self.check(real + 18 * DAY)  # and held
        for text in ("Atlaswick", "Orchidmarker", "Seedmarker1", "Seedmarker10"):
            self.assertTrue(self.memory.contains(text), text)


class GuestConversation(RetentionCase):
    cfg = {"identity": "You are Pip. You speak with Richard.", "core": f"The owner-only phrase is {OWNER_CORE}."}

    async def test_guest_start_and_end_isolate_owner_history_core_and_guest_chat(self):
        await self.seed_owner()
        owner_reminder = self.mind.skills.reminders.add("Read OwnerReminderMarker", time.time() + DAY)
        await self.say("Start guest mode")

        await self.say("Forget that")
        await self.say("Forget about my orchid")
        self.assertNotIn("Orchidmarker", await self.say("What do you remember about my orchid?"))
        self.assertNotIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        self.assertNotIn("OwnerReminderMarker", await self.say("What are my reminders?"))
        await self.say(GUEST_HISTORY)
        prompt = self.latest_prompt()
        self.assertIn("Kiteguest", prompt)
        for owner_text in ("Atlaswick", "Orchidmarker", OWNER_CORE, "Richard"):
            self.assertNotIn(owner_text, prompt)
        self.assertIn("Kiteguest", await self.say("What did I say about the amber kite?"))
        await self.say(f"Remember that {GUEST_FACT}")
        self.assertIn("Badgeguest", await self.say("What do you remember about my guest badge?"))
        await self.say("Remind me to read GuestReminderMarker tomorrow at 9 a.m.")
        self.assertEqual(self.mind.skills.reminders.upcoming(), [owner_reminder])

        await self.say("End guest mode")

        self.assertNotIn("Kiteguest", await self.say("What did I say about the amber kite?"))
        self.assertNotIn("Badgeguest", await self.say("What do you remember about my guest badge?"))
        self.assertIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        self.assertIn("Orchidmarker", await self.say("What do you remember about my orchid?"))
        self.assert_not_persisted("Kiteguest", "Badgeguest", "GuestReminderMarker")

    async def test_dont_remember_this_is_private_and_guest_data_does_not_survive_restart(self):
        await self.seed_owner()
        await self.say("Don't remember this.")
        slot_boundary = len(self.cortex_server.slot_calls)
        await self.say(GUEST_HISTORY)
        await self.say(f"Remember that {GUEST_FACT}")
        self.assertIn("Kiteguest", await self.say("What did I say about the amber kite?"))

        await self.restart()

        self.assertNotIn("Kiteguest", await self.say("What did I say about the amber kite?"))
        self.assertNotIn("Badgeguest", await self.say("What do you remember about my guest badge?"))
        self.assertIn("Orchidmarker", await self.say("What do you remember about my orchid?"))
        # The conversation cache is never saved. The identity snapshot made
        # by the fresh mind after the restart holds only the identity prompt.
        saved = [name for action, name in self.cortex_server.slot_calls[slot_boundary:] if action == "action=save"]
        self.assertEqual([name for name in saved if name != "identity.slot"], [])
        self.assert_not_persisted("Kiteguest", "Badgeguest")

    async def test_boot_local_guest_latch_survives_mind_restart_with_an_empty_guest_archive(self):
        runtime = self.root / "run"
        runtime.mkdir()
        marker = runtime / "guest"
        request = runtime / "settings-page"
        request.write_text("care\n")
        with patch.object(care_routes, "GUEST", marker), patch.object(care_routes, "REQUEST", request):
            self.guest_latch = True
            await self.restart()
            await self.seed_owner()
            await self.say("Start guest mode")
            self.assertTrue(marker.is_file())
            self.assertFalse(request.exists())
            await self.say(GUEST_HISTORY)
            await self.say(f"Remember that {GUEST_FACT}")

            await self.restart()

            topics = [event["t"] for event in self.bus.sent]
            self.assertLess(topics.index("app.privacy"), topics.index("mind.ready"))
            self.assertTrue(self.bus.of("app.privacy")[0]["guest"])
            self.assertTrue(self.mind.apps.guest)
            self.assertTrue(marker.is_file())
            self.assertNotIn(OWNER_CORE, self.mind.system)
            self.assertNotIn("Orchidmarker", await self.say("What do you remember about my orchid?"))
            self.assertNotIn("Kiteguest", await self.say("What did I say about the amber kite?"))
            self.assertNotIn("Badgeguest", await self.say("What do you remember about my guest badge?"))
            await self.say("Remember that this session is called Afterrestartguest")
            self.assert_not_persisted("Kiteguest", "Badgeguest", "Afterrestartguest")

            await self.say("End guest mode")
            self.assertFalse(marker.exists())
            self.assertFalse(self.bus.of("app.privacy")[-1]["guest"])
            self.assertIn("Orchidmarker", await self.say("What do you remember about my orchid?"))

    async def test_broken_guest_latch_keeps_owner_archive_closed(self):
        marker = self.root / "guest"
        marker.symlink_to(self.root / "missing")
        self.guest_latch = True
        with patch.object(care_routes, "GUEST", marker):
            await self.restart()
            self.assertTrue(self.mind.apps.guest)
            self.assertNotIn(OWNER_CORE, self.mind.system)

    async def test_guest_shortcut_discards_unpunctuated_attached_transcript(self):
        await self.seed_owner()
        await self.say("Don't remember this the cupboard is called Inlineguest")

        self.assertNotIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        self.assertNotIn("Inlineguest", await self.say("What did I say about the cupboard?"))
        await self.say("End guest mode")

        self.assertIn("Atlaswick", await self.say("What did I say about my bookshelf?"))
        self.assert_not_persisted("Inlineguest")

    async def test_guest_cortex_waits_for_cache_erase_acknowledgement(self):
        await self.seed_owner()
        cortex_slot = self.mind.cortex.slot
        erase_allowed = False

        async def slot(action, filename=SLOT_FILE):
            if action == "erase" and not erase_allowed:
                return False
            return await cortex_slot(action, filename)

        with patch.object(self.mind.cortex, "slot", side_effect=slot):
            await self.say("Start guest mode")
            model_requests = len(self.cortex_server.chats)
            await self.say(GUEST_HISTORY)
            self.assertEqual(len(self.cortex_server.chats), model_requests)
            self.assertIn("7:05", await self.say("What time is it?"))

            erase_allowed = True
            await self.say(GUEST_HISTORY)

        self.assertEqual(len(self.cortex_server.chats), model_requests + 1)
        self.assertIn("Kiteguest", self.latest_prompt())
        self.assertNotIn("Atlaswick", self.latest_prompt())
        self.assertNotIn(OWNER_CORE, self.latest_prompt())

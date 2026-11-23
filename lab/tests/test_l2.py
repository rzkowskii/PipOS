"""L2: it thinks and remembers.

A real language model runs inside the virtual laptop, built for its CPU. What it
is told survives a cold reboot. It knows how each wake began. And what it is
told to forget is gone from the disk itself.

The CPU is emulated, so the model is slow here. The waits below are patience,
not targets.
"""

import hashlib
import json
import shlex
import time
import unittest

from lab.vm import IMAGE, ROOT, VM

BOOT = 300
MODEL_LOAD = 1200
REPLY = 1500


def wake(vm):
    """Boot to the point where it is idle and its model answers."""
    vm.login(BOOT)
    vm.wait_state("idle", MODEL_LOAD)
    deadline = time.monotonic() + MODEL_LOAD
    while time.monotonic() < deadline:
        if "cortex is answering" in vm.run("journalctl -b -u being-mind --no-pager -o cat")[1]:
            return
        time.sleep(5)
    raise AssertionError("the model never started answering:\n" + vm.run("journalctl -b -u being-cortex --no-pager -o cat | tail -20")[1])


def life(vm):
    return json.loads(vm.run("cat /mind/life.json")[1])


def sleep(vm):
    """Shut down properly and wait for the machine to go off."""
    vm.send("poweroff\n")
    if not vm.wait_off(180):
        raise AssertionError("it did not power off")
    vm.stop()


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Thinking(unittest.TestCase):
    """One boot, looked at from several sides. Tests run in name order."""

    @classmethod
    def setUpClass(cls):
        cls.vm = VM().start()
        try:
            wake(cls.vm)
        except Exception:
            cls.vm.destroy()
            raise
        print(f"\n  power-on to idle with a working model: {time.monotonic() - cls.vm.started:.0f}s (emulated)")

    @classmethod
    def tearDownClass(cls):
        cls.vm.destroy()

    def sh(self, command, timeout=60):
        status, out = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"`{command}` failed:\n{out}")
        return out

    def test_1_every_organ_is_running(self):
        for unit in ("being-face", "being-brainstem", "being-mind", "being-cortex", "being-lab-body"):
            self.assertEqual(self.sh(f"systemctl is-active {unit} || true"), "active", unit)
        self.assertEqual(self.sh("systemctl show -p NRestarts --value being-cortex"), "0", "the model server crashed and restarted")

    def test_2_the_model_server_is_built_for_this_cpu(self):
        # The CPU has no AVX2. A build that used it would have died with an
        # illegal instruction instead of answering.
        flags = self.sh("grep -m1 '^flags' /proc/cpuinfo").split()
        self.assertIn("avx", flags)
        for unsupported in ("avx2", "fma", "bmi2"):
            self.assertNotIn(unsupported, flags)

        # llama-server only prints its build features at trace verbosity.
        # Probe startup with an empty model file to keep trace out of the service log.
        _, output = self.vm.run(
            "llama-server --log-verbosity 4 --host 127.0.0.1 --port 0 --model /dev/null --no-ui 2>&1",
            timeout=20,
        )
        info = next((line for line in output.splitlines() if "system_info:" in line), "")
        self.assertIn("AVX = 1", info, output)
        self.assertNotIn("AVX2 = 1", info)
        self.assertNotIn("FMA = 1", info)
        self.assertNotIn("BMI2 = 1", info)

    def test_3_it_was_born_on_this_first_boot(self):
        record = life(self.vm)
        self.assertEqual(record["wakes"], 1)
        self.assertEqual(len(record["id"]), 32)
        self.assertTrue(record["this_wake"]["first"])
        self.assertIn("QEMU", record["body"])
        self.assertIn("born just now", self.sh("journalctl -b -u being-brainstem --no-pager -o cat"))
        self.assertIn("discard", self.sh("grep ' /mind ' /proc/mounts"))

    def test_4_it_answers_with_a_real_model(self):
        started = time.monotonic()
        reply = self.vm.converse("Tell me about the sea.", REPLY)
        print(f"\n  it said: {reply!r}  ({time.monotonic() - started:.0f}s, emulated)")
        self.assertGreater(len(reply.split()), 1, "it said nothing")
        self.assertNotIn("[", reply, "an emotion tag was spoken aloud")
        self.assertNotIn("can't think right now", reply)
        self.assertNotIn("lost my thought", reply)
        log = self.sh("journalctl -b -u being-brainstem --no-pager -o cat")
        for step in ("idle -> attending", "attending -> listening", "listening -> thinking", "thinking -> speaking", "speaking -> attending"):
            self.assertIn(step, log)

    def test_5_commands_are_answered_without_the_model(self):
        started = time.monotonic()
        self.assertEqual(self.vm.converse("Remember that my cat is called Tom", 300), "Okay. I'll remember that.")
        self.assertEqual(self.vm.converse("What do you remember about my cat?", 300), "You told me: my cat is called Tom.")
        self.assertLess(time.monotonic() - started, 120, "commands should not wait on the model")

    def test_6_it_tells_the_time_from_its_own_clock(self):
        reply = self.vm.converse("What time is it?", 300)
        # Pip answers in the owner's time zone (config/being.json), not UTC.
        zone = json.loads((ROOT / "config/being.json").read_text())["time_zone"]
        hour, minute = (int(x) for x in self.sh(f"TZ={zone} date +%H:%M").split(":"))
        minutes = [f"{(hour % 12) or 12}:{m % 60:02d}" for m in (minute - 1, minute, minute + 1)]
        self.assertTrue(any(f"It's {m} " in reply for m in minutes), f"{reply!r} is not the machine's time {hour}:{minute:02d}")


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Continuity(unittest.TestCase):
    def test_one_life_across_reboots_a_power_cut_and_a_new_body(self):
        vm = VM()
        try:
            # First wake: it is told something, then goes to sleep properly.
            vm.start()
            vm.login(BOOT)
            vm.wait_state("idle", MODEL_LOAD)
            self.assertEqual(vm.converse("Remember that my cat is called Tom", 300), "Okay. I'll remember that.")
            born = life(vm)
            sleep(vm)

            # Second wake, after a proper sleep: same being, and it remembers.
            vm.start()
            vm.login(BOOT)
            vm.wait_state("idle", MODEL_LOAD)
            second = life(vm)
            self.assertEqual((second["id"], second["born"]), (born["id"], born["born"]))
            self.assertEqual(second["wakes"], 2)
            self.assertFalse(second["this_wake"]["lost_power"])
            self.assertIsNotNone(second["this_wake"]["slept"])
            self.assertEqual(vm.converse("What do you remember about my cat?", 300), "You told me: my cat is called Tom.")

            # The plug is pulled while it is awake.
            vm.power_cut()
            vm.start()
            vm.login(BOOT)
            vm.wait_state("idle", MODEL_LOAD)
            third = life(vm)
            self.assertEqual(third["wakes"], 3)
            self.assertTrue(third["this_wake"]["lost_power"])
            self.assertIn("after losing power", vm.run("journalctl -b -u being-brainstem --no-pager -o cat")[1])
            # Its memory came through the power cut.
            self.assertEqual(vm.converse("What do you remember about my cat?", 300), "You told me: my cat is called Tom.")
            sleep(vm)

            # The same disk wakes in a machine that says it is the laptop.
            vm.maker = ("Apple Inc.", "MacBookPro9,2")
            vm.start()
            vm.login(BOOT)
            vm.wait_state("idle", MODEL_LOAD)
            fourth = life(vm)
            self.assertEqual(fourth["id"], born["id"])
            self.assertTrue(fourth["this_wake"]["new_body"])
            self.assertFalse(fourth["this_wake"]["lost_power"])
            self.assertIn("MacBookPro9,2", fourth["body"])
            # The mind wrote each wake into its own log.
            rows = vm.run(
                "python3 -c \"import sqlite3; print([r[0] for r in sqlite3.connect('/mind/mind.db')"
                ".execute(\\\"select text from episodes where kind='life' order by id\\\")])\""
            )[1]
            self.assertEqual(rows, "['born', 'woke', 'woke after losing power', 'woke in a new body']")
        finally:
            vm.destroy()


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Forgetting(unittest.TestCase):
    def test_casual_conversation_can_be_recalled_edited_and_forgotten(self):
        vm = VM()
        try:
            vm.start()
            wake(vm)
            status, output = vm.run("systemctl stop being-cortex")
            self.assertEqual(status, 0, output)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if "cortex stopped answering" in vm.run("journalctl -b -u being-mind --no-pager -o cat")[1]:
                    break
                time.sleep(1)
            else:
                self.fail("the mind did not notice that the model stopped")
            self.assertIn("can't think right now", vm.converse("I call my orchid Velvetbox.", 25))
            self.assertEqual(vm.converse("Remember that the bike is called Zanzibarine", 300), "Okay. I'll remember that.")
            status, output = vm.run(
                "systemctl stop being-mind && python3 -c 'import json; from pathlib import Path; "
                "p = Path(\"/mind/context.json\"); data = json.loads(p.read_text()); "
                "data[\"saved\"] = 0; p.write_text(json.dumps(data))'"
            )
            self.assertEqual(status, 0, output)
            sleep(vm)

            vm.start()
            wake(vm)
            recall = "What did I say about orchids?"
            self.assertIn("I call my orchid Velvetbox", vm.converse(recall, 45))
            self.assertEqual(
                vm.converse("That's wrong, I call my orchid Silverleaf", 45), "Got it. I've changed that."
            )
            self.assertIn("I call my orchid Silverleaf", vm.converse(recall, 45))
            self.assertEqual(vm.converse("Forget that", 45), "Okay. That's forgotten.")
            self.assertEqual(vm.converse(recall, 45), "I don't remember anything about that.")
            sleep(vm)

            self.assertTrue(vm.disk_contains(b"zanzibarin"), "retained fact is the scan control")
            self.assertFalse(vm.disk_contains(b"velvetbox"), "the original wording is still on disk")
            self.assertFalse(vm.disk_contains(b"silverleaf"), "the forgotten wording is still on disk")
        finally:
            vm.destroy()

    def test_what_it_forgets_is_gone_from_the_disk(self):
        vm = VM()
        try:
            vm.start()
            wake(vm)
            # One thing to keep, as proof the search below can find things.
            self.assertEqual(vm.converse("Remember that my bike is called Zanzibarine", 300), "Okay. I'll remember that.")
            # One thing to forget.
            self.assertEqual(vm.converse("Remember that the safe word is Quixotically", 300), "Okay. I'll remember that.")
            # Talk about it with the model, so it is also in the live
            # conversation, the saved context and the model's cache.
            reply = vm.converse("Tell me about the safe word.", REPLY)
            self.assertTrue(reply)
            self.assertNotIn("lost my thought", reply)
            self.assertNotIn("can't think right now", reply)
            context = vm.run("cat /mind/context.json")[1]
            self.assertIn("Quixotically", context, "the fact should have been recalled into the conversation")

            self.assertEqual(vm.converse("Forget that", 300), "Okay. That's forgotten.")
            self.assertEqual(
                vm.converse("What do you remember about the safe word?", 300), "I don't remember anything about that."
            )
            sleep(vm)

            self.assertTrue(vm.disk_contains(b"zanzibarin"), "the scan cannot find even what was kept")
            self.assertFalse(vm.disk_contains(b"quixot"), "the forgotten words are still on the disk")
        finally:
            vm.destroy()


@unittest.skipUnless(IMAGE.exists(), "no image: run lab/build.sh first")
class Compaction(unittest.TestCase):
    def setUp(self):
        self.vm = VM()
        self.addCleanup(self.vm.destroy)

    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"`{command}` failed:\n{output}")
        return output

    def context(self):
        return json.loads(self.sh("cat /mind/context.json"))

    def wait_guest(self, command, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, output = self.vm.run(command, 10)
            if status == 0:
                return output
            time.sleep(0.3)
        self.fail(f"timed out waiting for `{command}`:\n{output}")

    def wait_mind_ready(self):
        self.wait_guest(
            'pid=$(systemctl show -p MainPID --value being-mind) && test "$pid" -gt 0 && '
            'journalctl -b -u being-mind _PID="$pid" --no-pager -o cat | grep -q "cortex is answering"'
        )

    def boot(self, *, install_fixture=False):
        self.vm.start()
        self.vm.login(BOOT)
        if install_fixture:
            sources = ("consolidation.py", "memory.py", "turn.py")
            guest_dir = "/usr/lib/being/mind"
            hashes = self.sh("sha256sum " + " ".join(f"{guest_dir}/{name}" for name in sources))
            actual = {path: digest for digest, path in (line.split() for line in hashes.splitlines())}
            expected = {
                f"{guest_dir}/{name}": hashlib.sha256((ROOT / "mind" / name).read_bytes()).hexdigest()
                for name in sources
            }
            self.assertEqual(actual, expected)
            print(f"\n  packaged memory sources:\n{hashes}", flush=True)
        self.sh("systemctl stop being-mind being-cortex", timeout=90)
        if install_fixture:
            # Use the packaged mind and serial body with a deterministic model.
            # The prompt observer writes to tmpfs so it cannot affect the disk scan.
            fixture = (ROOT / "sim" / "fake_cortex.py").read_text().split('if __name__ == "__main__":', 1)[0]
            fixture += '''\nfrom pathlib import Path

def observe(messages):
    Path("/run/compaction-prompt").write_text(messages[0]["content"])
    return "[neutral] I hear you."

FakeCortex(port=8080, delay=0, reply=observe).server.serve_forever()
'''
            self.vm.put("/mind/.compaction-cortex.py", fixture)
        self.sh("mkdir -p /run/systemd/system/being-cortex.service.d")
        self.vm.put("/run/systemd/system/being-cortex.service.d/compaction.conf", """[Service]
ExecStart=
ExecStart=/usr/bin/python3 /mind/.compaction-cortex.py
""")
        self.sh("systemctl daemon-reload && systemctl start being-cortex being-mind")
        self.wait_mind_ready()
        self.vm.wait_state("idle", 40)

    def prompt(self):
        self.assertEqual(self.vm.converse("Tell me about the sea.", 25), "I hear you.")
        return self.sh("cat /run/compaction-prompt")

    def test_archive_highlights_survive_reboot_correction_and_forgetting(self):
        omitted = "My telescope is called Heliograin."
        original = "My mandolin is called Zaframoon."
        corrected = "My mandolin is called Sablewillow"
        archive_turns = [omitted, "My atlas is called Velora.", original, "My kettle is called Copperglow."]
        recall = "What did I say about the mandolin?"
        self.boot(install_fixture=True)
        self.assertEqual(self.vm.converse("Remember that my bicycle is called Beryltrident", 25),
                         "Okay. I'll remember that.")
        for text in archive_turns + ["Tell me about the sea."] * 4:
            self.assertEqual(self.vm.converse(text, 25), "I hear you.")

        before = self.context()
        self.assertEqual(len(before["messages"]), 16)
        selected = {m["episode_id"] for m in before["messages"][:8]
                    if m["role"] == "user" and any(text in m["content"] for text in archive_turns[1:])}
        self.assertEqual(len(selected), 3)
        source = next(m["episode_id"] for m in before["messages"] if original in m["content"])

        # Lower the configured budget only after capturing the short test conversation.
        # This lets real idle housekeeping compact it without a long workload.
        config_override = (
            "import json; from pathlib import Path; "
            "data = json.loads(Path('/etc/being/being.json').read_text()); data['ctx_tokens'] = 256; "
            "Path('/run/compaction-config.json').write_text(json.dumps(data))"
        )
        self.sh(f"systemctl stop being-mind && python3 -c {shlex.quote(config_override)} && "
                "mount -o bind /run/compaction-config.json /etc/being/being.json && systemctl start being-mind")
        self.wait_mind_ready()
        self.vm.voice._cursor = self.vm.voice.mark()
        self.vm.say("Good night")
        self.vm.wait_state("asleep", 30)
        self.wait_guest("journalctl -b -u being-mind --no-pager -o cat | grep -q 'compacted 8 messages with 3 archive sources'")
        self.wait_guest("grep -Eq '\"highlights\": \\[[0-9]' /mind/context.json")
        compacted = self.context()
        self.assertEqual(set(compacted["highlights"]), selected)
        self.assertEqual(len(compacted["messages"]), 8)
        self.assertNotIn(original, json.dumps(compacted))
        print("  first boot compacted four exchanges into three archive links", flush=True)
        sleep(self.vm)

        self.boot()
        self.assertEqual(self.context()["highlights"], compacted["highlights"])
        prompt = self.prompt()
        self.assertIn(original, prompt)
        self.assertNotIn(omitted, prompt)
        self.assertIn(omitted, self.vm.converse("What did I say about the telescope?", 25))
        self.assertIn(original, self.vm.converse(recall, 25))
        self.assertEqual(self.vm.converse(f"That's wrong, {corrected}", 25), "Got it. I've changed that.")
        print("  second boot restored the highlights and found the omitted archive entry", flush=True)
        sleep(self.vm)

        self.boot()
        self.assertIn(source, self.context()["highlights"])
        prompt = self.prompt()
        self.assertIn(corrected, prompt)
        self.assertNotIn(original, prompt)
        self.assertIn(corrected, self.vm.converse(recall, 25))
        self.assertEqual(self.vm.converse("Forget that", 25), "Okay. That's forgotten.")
        print("  third boot restored the corrected source before forgetting it", flush=True)
        sleep(self.vm)

        self.boot()
        self.assertNotIn(source, self.context()["highlights"])
        prompt = self.prompt()
        self.assertNotIn(original, prompt)
        self.assertNotIn(corrected, prompt)
        self.assertEqual(self.vm.converse(recall, 25), "I don't remember anything about that.")
        self.assertIn("Beryltrident", self.vm.converse("What do you remember about my bicycle?", 25))
        print("  fourth boot reported the target forgotten and recalled the control", flush=True)
        sleep(self.vm)

        self.assertTrue(self.vm.disk_contains(b"beryltrident"), "retained fact is the scan control")
        self.assertFalse(self.vm.disk_contains(b"zaframoon"), "the original wording is still on disk")
        self.assertFalse(self.vm.disk_contains(b"sablewillow"), "the forgotten wording is still on disk")


if __name__ == "__main__":
    unittest.main()

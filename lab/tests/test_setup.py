"""Follow first-run choices from a serial console to the running being."""

import json
import os
import re
import time
import unittest
from pathlib import Path

from lab.vm import IMAGE, VM

SETUP_IMAGE = Path(os.environ.get("BEING_SETUP_IMAGE", IMAGE)).resolve()
CHOICE = r"Choice: "
ROLL = r"\[Q\] Discard roll: "

# The microphone is substituted only at the transcript boundary. The
# installed mind, voice, Flite and ALSA loopback handle the reply.
TALK = r"""
import json
import socket

with socket.socket(socket.AF_UNIX) as bus:
    bus.settimeout(75)
    bus.connect("/run/being/bus.sock")

    def send(topic, **fields):
        bus.sendall((json.dumps({"t": topic, **fields}) + "\n").encode())

    send("hello", name="setup-test", sub=["state.changed", "say.*"])
    with bus.makefile() as stream:
        events = (json.loads(line) for line in stream)

        def wait_for_state(*states):
            return next(event for event in events if event["t"] == "state.changed"
                        and event["state"] in states and not event["unwell"])

        wait_for_state("idle", "drowsy", "asleep")
        bus.settimeout(25)
        send("hear.wake")
        wait_for_state("attending")
        for question in ("What is your name?", "Hello", "What do you remember about my atlas?"):
            send("hear.final", text=question)
            heard = []
            started = audible = False
            for event in events:
                topic = event["t"]
                if topic == "say.request" and event["text"]:
                    heard.append(event["text"])
                elif topic == "say.started":
                    started = True
                elif topic == "say.level" and event["v"] > 0:
                    audible = True
                elif topic == "say.done" and event.get("final"):
                    break
            print(json.dumps({"text": " ".join(heard), "played": started and audible}), flush=True)
            wait_for_state("attending")
"""


@unittest.skipUnless(SETUP_IMAGE.exists(), "no image: run lab/build.sh first")
class Personalization(unittest.TestCase):
    def sh(self, command, timeout=30):
        status, output = self.vm.run(command, timeout)
        self.assertEqual(status, 0, f"{command}:\n{output}")
        return output

    def profile(self):
        return json.loads(self.sh("being-setup --show"))

    def choose(self, value, prompt=CHOICE):
        self.vm.send(f"{value}\n")
        return self.vm.expect(prompt, 30)

    def setup_menu(self):
        self.vm.send('being-setup; echo "<<setup:$?>>"\n')
        self.vm.expect(CHOICE, 30)

    def save(self):
        self.choose("y", r"<<setup:0>>")

    def life(self):
        command = 'grep -Fq "$(cat /proc/sys/kernel/random/boot_id)" /mind/life.json && cat /mind/life.json'
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status, output = self.vm.run(command, 10)
            if status == 0:
                return json.loads(output)
            time.sleep(0.3)
        self.fail(f"The life record did not update for this boot:\n{output}")

    def wait_voice(self):
        command = "journalctl -b -u being-voice --no-pager -o cat"
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            log = self.sh(command)
            if "Playback voice: awb at 0.85x" in log:
                return
            time.sleep(0.3)
        self.fail(f"Voice did not load the saved profile:\n{log}")

    def test_wizard_persists_preferences_and_life_across_reboot(self):
        self.vm = VM(image=SETUP_IMAGE)
        self.addCleanup(self.vm.destroy)
        self.vm.start()
        self.vm.login(120)
        self.vm.wait_state("idle", 150)
        factory = self.profile()
        self.assertEqual((factory["name"], factory["wake_word"], factory["voice"]), ("Pip", "Hey Pip", "slt"))
        born = self.life()
        self.assertEqual(self.vm.converse("Remember that my atlas is called Velora", 30), "Okay. I'll remember that.")

        self.setup_menu()
        self.choose("1", r"Name \[Pip\]: ")
        self.choose("Fred")
        voices = self.choose("4", r"(?s)(.*?)Choice: ").group(1)
        for label in ("Pip's original voice", "Deeper voice", "Scottish English", "Retro synthetic"):
            self.assertIn(label, voices)
        awb = re.search(r"(?m)^  (\d+)  Scottish English\r?$", voices)
        self.assertIsNotNone(awb, voices)
        self.choose(awb[1])
        self.choose("5")
        self.choose("1")
        self.choose("6")
        self.choose("5", ROLL)
        self.choose("r", ROLL)
        self.choose("k", r"Save these choices\? \[y/N\] ")
        self.save()
        rolled = self.profile()
        self.assertEqual((rolled["name"], rolled["wake_word"], rolled["voice"], rolled["rate"]),
                         ("Fred", "Hey Pip", "awb", 0.85))
        self.assertEqual(set(rolled["personality"]), {"warmth", "curiosity", "humour", "expressiveness", "detail"})
        self.assertTrue(all(type(value) is int and 0 <= value <= 2 for value in rolled["personality"].values()))

        self.setup_menu()
        self.choose("6")
        self.choose("2")
        self.choose("k", r"Save these choices\? \[y/N\] ")
        self.save()
        saved = self.profile()
        print("\n  first boot saved the rolled personality and then the Calm preset", flush=True)

        self.vm.send("poweroff\n")
        self.assertTrue(self.vm.wait_off(90), "the first boot did not power off cleanly")
        self.vm.stop()
        self.vm.loopback = True
        self.vm.start()
        self.vm.login(120)
        self.assertEqual(self.profile(), saved)
        woke = self.life()
        self.assertEqual((woke["id"], woke["born"], woke["wakes"]), (born["id"], born["born"], born["wakes"] + 1))
        self.assertFalse(woke["this_wake"]["lost_power"])
        self.wait_voice()
        print("  second boot retained the profile and life record and loaded the Scottish voice", flush=True)
        self.vm.put("/run/being-setup-probe.py", TALK)
        replies = [json.loads(line) for line in self.sh("python3 /run/being-setup-probe.py", 150).splitlines()]
        self.assertEqual([reply["text"] for reply in replies],
                         ["My name is Fred.", "Hello. I'm listening.", "You told me: my atlas is called Velora."])
        self.assertTrue(all(reply["played"] for reply in replies), replies)


if __name__ == "__main__":
    unittest.main()

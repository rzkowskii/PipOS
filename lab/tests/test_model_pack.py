"""Exercise the signed model gate through the candidate's actual Cortex unit,
the role packs, the verdict cache and the two-slot cortex."""

import unittest

from lab.vm import IMAGE, VM


@unittest.skipUnless(IMAGE.exists(), "requires the candidate factory image")
class SignedModelPack(unittest.TestCase):
    def test_tampered_signature_and_weights_block_cortex_until_pack_is_restored(self):
        def check(command, timeout=120):
            code, output = guest.run(command, timeout=timeout)
            self.assertEqual(code, 0, command + "\n" + output)
            return output

        with VM(image=IMAGE) as guest:
            guest.login(180)
            check("systemctl start being-cortex && systemctl is-active being-cortex", 240)
            check("systemctl stop being-cortex && printf changed >/run/bad-pack")
            for file, reason in (("beingos-conversation.cms", "invalid model envelope"),
                                 ("reflex.gguf", "model size differs")):
                with self.subTest(file=file):
                    check(f"mount --bind /run/bad-pack /models/{file}")
                    check("systemctl start being-cortex || true")
                    self.assertEqual(check("systemctl show -p MainPID --value being-cortex"), "0")
                    self.assertIn("Model pack rejected: " + reason,
                                  check("journalctl -b -u being-cortex --no-pager -o cat"))
                    check(f"umount /models/{file}")
            check("systemctl start being-cortex && systemctl is-active being-cortex", 240)
            check("python3 -c 'import time,urllib.request\n"
                  "for i in range(90):\n"
                  " try: urllib.request.urlopen(\"http://127.0.0.1:8080/health\",timeout=2); break\n"
                  " except OSError: time.sleep(1)\n"
                  "else: raise SystemExit(1)'", 120)


@unittest.skipUnless(IMAGE.exists(), "requires the candidate factory image")
class RolePacksAndSlots(unittest.TestCase):
    def test_every_role_verifies_and_the_verdict_skips_the_second_hash(self):
        def check(command, timeout=120):
            code, output = guest.run(command, timeout=timeout)
            self.assertEqual(code, 0, command + "\n" + output)
            return output

        with VM(image=IMAGE) as guest:
            guest.login(180)
            for role in ("conversation", "dream", "vad", "asr", "tts", "embeddings", "speaker"):
                with self.subTest(role=role):
                    self.assertIn("Qualified", check(f"python3 /usr/lib/being/mind/model_pack.py verify --role {role}"))
            check("systemctl start being-cortex && test -s /mind/cortex/pack-verdict.json", 240)
            timed = check("python3 -c 'import time,subprocess; t=time.monotonic(); "
                          "subprocess.run([\"python3\",\"/usr/lib/being/mind/model_pack.py\",\"verify\"],check=True); "
                          "print(round(time.monotonic()-t,2))'")
            self.assertLess(float(timed.split()[-1]), 1.5, "a cached verdict should skip the full hash")

    def test_a_slot_1_job_leaves_slot_0_cache(self):
        """The L7.1 probe: slot 0's cache_n is unchanged after a code job on slot 1."""
        probe = (
            "import json,urllib.request\n"
            "def post(path, body):\n"
            " r = urllib.request.Request('http://127.0.0.1:8080' + path, json.dumps(body).encode(),"
            " {'Content-Type': 'application/json'})\n"
            " return json.loads(urllib.request.urlopen(r, timeout=300).read())\n"
            "chat = '<|im_start|>system\\nYou are Pip.<|im_end|>\\n<|im_start|>user\\nhello<|im_end|>\\n'"
            "'<|im_start|>assistant\\n'\n"
            "post('/completion', {'prompt': chat, 'n_predict': 1, 'cache_prompt': True, 'id_slot': 0})\n"
            "before = post('/completion', {'prompt': chat, 'n_predict': 1, 'cache_prompt': True, 'id_slot': 0})\n"
            "post('/completion', {'prompt': 'def count():' * 40, 'n_predict': 16, 'cache_prompt': True, 'id_slot': 1})\n"
            "after = post('/completion', {'prompt': chat, 'n_predict': 1, 'cache_prompt': True, 'id_slot': 0})\n"
            "print(before['timings']['cache_n'], after['timings']['cache_n'])\n"
        )
        with VM(image=IMAGE) as guest:
            guest.login(180)
            guest.put("/run/slot-probe.py", probe)
            code, output = guest.run("systemctl start being-cortex && python3 -c 'import time,urllib.request\n"
                                     "for i in range(90):\n"
                                     " try: urllib.request.urlopen(\"http://127.0.0.1:8080/health\",timeout=2); break\n"
                                     " except OSError: time.sleep(1)' && python3 /run/slot-probe.py", timeout=600)
            self.assertEqual(code, 0, output)
            before, after = map(int, output.split()[-2:])
            self.assertGreater(before, 0)
            self.assertEqual(after, before)

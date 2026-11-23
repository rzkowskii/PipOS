"""Exercise the signed model gate through the candidate's actual Cortex unit."""

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

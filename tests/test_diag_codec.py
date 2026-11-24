"""pip-diag's codec reading and live experiments, against the MacBookPro9,2's CS4206.

The kernel log, alsactl and mixer lines are copied from the v5 hardware boot
(out/hardware-logs/v5-boot-2026-11-24: dmesg.txt:733, journal.txt:1034,
hardware-report.txt:616-640). That boot saved no full codec dump, so the
dump below is the kernel's /proc/asound/card0/codec#0 format filled in with
the IDs from that boot and the node values the investigation found (pin
0x0d 0x90a00110 with VREF_80; node 0x11 coefficient 0x02 at the generic
0x000a). No serial numbers or hardware addresses are included.
"""

import struct
import tempfile
import unittest
from pathlib import Path

from mind import diag_codec as codec
from mind.diag_system import Result
from tests.test_diag import FakeHost

V5_DMESG = """\
[    1.445061] snd_hda_codec_cs420x hdaudioC0D0: CS4206: picked fixup  for codec SSID 106b:0000
[    1.445362] snd_hda_codec_cs420x hdaudioC0D0: autoconfig for CS4206: line_outs=2 (0xb/0xa/0x0/0x0/0x0) type:speaker
[    1.445405] snd_hda_codec_cs420x hdaudioC0D0:      Mic=0xd
[    6.942430] snd_hda_intel 0000:00:1b.0: IRQ timing workaround is activated for card #0. Suggest a bigger bdl_pos_adj.
"""
V5_ALSACTL = ('[    5.385508] being alsactl[259]: Found hardware: "HDA-Intel" "Cirrus Logic CS4206" '
              '"HDA:10134206,106b5200,00100302 HDA:80862806,80860101,00100000" "0x8086" "0x7270"\n')
V5_MIXER = """\
Simple mixer control 'Mic Boost',0
  Capabilities: volume
  Playback channels: Front Left - Front Right
  Capture channels: Front Left - Front Right
  Limits: 0 - 3
  Front Left: 2 [67%] [20.00dB]
  Front Right: 2 [67%] [20.00dB]
Simple mixer control 'Capture',0
  Capabilities: cvolume cswitch
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 63
  Front Left: Capture 54 [86%] [3.00dB] [on]
  Front Right: Capture 54 [86%] [3.00dB] [on]
"""
CODEC_DUMP = """\
Codec: Cirrus Logic CS4206
Address: 0
AFG Function Id: 0x1 (unsol 1)
Vendor Id: 0x10134206
Subsystem Id: 0x106b5200
Revision Id: 0x100302
No Modem Function Group found
Default PCM:
    rates [0x7f0]: 32000 44100 48000 88200 96000 176400 192000
    bits [0xe]: 16 20 24
    formats [0x1]: PCM
State of AFG node 0x01:
  Power states:  D0 D3 CLKSTOP EPSS
  Power: setting=D0, actual=D0
GPIO: io=4, o=0, i=0, unsolicited=1, wake=0
  IO[0]: enable=0, dir=0, wake=0, sticky=0, data=0, unsol=0
  IO[1]: enable=1, dir=1, wake=0, sticky=0, data=0, unsol=0
  IO[2]: enable=0, dir=0, wake=0, sticky=0, data=0, unsol=0
  IO[3]: enable=1, dir=1, wake=0, sticky=0, data=1, unsol=0
Node 0x02 [Audio Output] wcaps 0x1d: Stereo Amp-Out
  Amp-Out caps: ofs=0x7f, nsteps=0x7f, stepsize=0x01, mute=1
  Amp-Out vals:  [0x6c 0x6c]
  Converter: stream=0, channel=0
  PCM:
    rates [0x7f0]: 32000 44100 48000 88200 96000 176400 192000
    bits [0xe]: 16 20 24
    formats [0x1]: PCM
  Power: setting=D0, actual=D0
Node 0x03 [Audio Output] wcaps 0x1d: Stereo Amp-Out
  Amp-Out caps: ofs=0x7f, nsteps=0x7f, stepsize=0x01, mute=1
  PCM:
    rates [0x60]: 44100 48000
    bits [0xe]: 16 20 24
    formats [0x1]: PCM
Node 0x06 [Audio Input] wcaps 0x10051b: Stereo Amp-In
  Amp-In caps: ofs=0x00, nsteps=0x00, stepsize=0x00, mute=1
  Amp-In vals:  [0x00 0x00] [0x80 0x80]
  Converter: stream=0, channel=0
  SDI-Select: 0
  PCM:
    rates [0x7f0]: 32000 44100 48000 88200 96000 176400 192000
    bits [0xe]: 16 20 24
    formats [0x1]: PCM
  Connection: 2
     0x0d* 0x0e
Node 0x0a [Pin Complex] wcaps 0x400501: Stereo
  Pincap 0x00000010: OUT
  Pin Default 0x90100112: [Fixed] Speaker at Int N/A
    Conn = Analog, Color = Unknown
  Pin-ctls: 0x40: OUT
  Connection: 1
     0x03
Node 0x0b [Pin Complex] wcaps 0x400501: Stereo
  Pincap 0x00000010: OUT
  Pin Default 0x90100110: [Fixed] Speaker at Int N/A
  Pin-ctls: 0x40: OUT
  Connection: 1
     0x02
Node 0x0d [Pin Complex] wcaps 0x40048b: Stereo Amp-In
  Amp-In caps: ofs=0x00, nsteps=0x03, stepsize=0x27, mute=0
  Amp-In vals:  [0x02 0x02]
  Pincap 0x00001724: IN Detect
    Vref caps: HIZ 50 GRD 80
  Pin Default 0x90a00110: [Fixed] Mic at Int N/A
    Conn = Analog, Color = Unknown
    DefAssociation = 0x1, Sequence = 0x0
    Misc = NO_PRESENCE
  Pin-ctls: 0x24: IN VREF_80
Node 0x11 [Vendor Defined Widget] wcaps 0xf00000: Mono
  Processing caps: benign=0, ncoeff=27
    Coeff 0x00: 0x0008
    Coeff 0x01: 0x0000
    Coeff 0x02: 0x000a
    Coeff 0x03: 0x0000
"""


class Parsing(unittest.TestCase):
    def setUp(self):
        self.codec = codec.parse_codec(CODEC_DUMP)

    def test_ids(self):
        self.assertEqual(self.codec["codec"], "Cirrus Logic CS4206")
        self.assertEqual(self.codec["vendor_id"], 0x10134206)
        self.assertEqual(self.codec["subsystem_id"], 0x106B5200)
        self.assertEqual(self.codec["revision_id"], 0x100302)

    def test_the_adc_configuration_coefficient_and_the_mic_pin(self):
        nodes = self.codec["nodes"]
        self.assertEqual(nodes[0x11]["coeffs"][0x02], 0x000A)
        self.assertEqual(nodes[0x0D]["pin_default"], 0x90A00110)
        self.assertEqual(nodes[0x0D]["pin_ctls"], 0x24)
        self.assertEqual(codec.describe_vref(nodes[0x0D]["pin_ctls_text"]), "80")
        self.assertEqual(nodes[0x06]["connections"], [(0x0D, True), (0x0E, False)])
        self.assertEqual(nodes[0x0A]["connections"], [(0x03, False)])

    def test_gpio_and_dac_rates(self):
        self.assertEqual(self.codec["gpio"]["io"], 4)
        self.assertEqual(self.codec["gpio"]["lines"][3]["data"], 1)
        self.assertEqual(self.codec["nodes"][0x03]["rates"], [44100, 48000])
        self.assertEqual(self.codec["nodes"][0x02]["bits"], [16, 20, 24])

    def test_the_v5_fixup_line_is_the_vendor_wide_apple_quirk(self):
        fixup = codec.picked_fixup(V5_DMESG)
        self.assertEqual(fixup["chip"], "CS4206")
        self.assertEqual(fixup["name"], "")
        self.assertEqual(fixup["ssid"], "106b:0000")
        self.assertIn("CS420X_APPLE", fixup["meaning"])

    def test_a_debug_kernel_names_the_fixup(self):
        fixup = codec.picked_fixup("snd_hda_codec_cs420x hdaudioC0D0: CS4206: picked fixup mbp81 (model specified)")
        self.assertEqual((fixup["name"], fixup["reason"], fixup["ssid"]), ("mbp81", "model specified", None))
        self.assertIsNone(codec.picked_fixup("nothing here"))

    def test_alsactl_ids(self):
        self.assertEqual(codec.alsactl_ids(V5_ALSACTL)[0], (0x10134206, 0x106B5200, 0x00100302))

    def test_an_empty_dump_parses_to_nothing(self):
        empty = codec.parse_codec("")
        self.assertEqual(empty["nodes"], {})
        self.assertIsNone(empty["vendor_id"])


class Checks(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def host(self, dump=CODEC_DUMP, *, sysfs=True, live=False):
        host = FakeHost(self.root, programs={"dmesg"},
                        handlers={"dmesg": lambda argv: Result(0, V5_DMESG + V5_ALSACTL)})
        host.made("/proc/asound/card0/codec#0", dump)
        if sysfs:
            host.made(f"{codec.SYSFS_CODEC}/subsystem_id", "0x106b5200\n")
            host.made(f"{codec.INTEL_PARAMS}/model", "\n")
            host.made(f"{codec.INTEL_PARAMS}/bdl_pos_adj", "-1\n")
        host.made(codec.DUMP_COEF, "-1\n")
        if live:
            host.made(codec.HWDEP, "")
            host.made(f"{codec.HWDEP_SYSFS}/reconfig", "")
            host.made(f"{codec.HWDEP_SYSFS}/modelname", "\n")
        return host

    def by_id(self, items):
        return {item["id"]: item for item in items}

    def test_no_sound_card_is_one_skip(self):
        items = codec.codec_checks(FakeHost(self.root))
        self.assertEqual([(item["id"], item["status"]) for item in items], [("codec.present", "skip")])

    def test_a_card_without_codec_zero_skips(self):
        host = FakeHost(self.root)
        host.made("/proc/asound/cards", "")
        self.assertEqual(codec.codec_checks(host)[0]["status"], "skip")

    def test_this_mac_on_the_generic_fixup_warns_with_the_way_forward(self):
        host = self.host()
        found = self.by_id(codec.codec_checks(host))
        self.assertEqual(found["codec.ssid"]["value"], "0x106b5200")
        self.assertIn("alsactl saw 0x10134206,0x106b5200", found["codec.ssid"]["evidence"])
        self.assertEqual(found["codec.fixup"]["status"], "warn")
        self.assertEqual(found["codec.adc_cfg"]["status"], "warn")
        self.assertIn("mbp81", found["codec.adc_cfg"]["hint"])
        self.assertEqual(found["codec.mic_pin"]["value"]["vref"], "80")
        self.assertIn("selected the mic pin", found["codec.adc_connection"]["evidence"])
        self.assertEqual(found["codec.live"]["status"], "skip")
        self.assertEqual(found["codec.irq_timing"]["value"], 1)
        # dump_coef was turned on for the read and put back after.
        self.assertEqual(host.path(codec.DUMP_COEF).read_text(), "-1")

    def test_the_mbp81_fixup_passes(self):
        found = self.by_id(codec.codec_checks(self.host(CODEC_DUMP.replace("Coeff 0x02: 0x000a",
                                                                          "Coeff 0x02: 0x102a"))))
        self.assertEqual(found["codec.adc_cfg"]["status"], "pass")
        self.assertIn("single ended", found["codec.adc_cfg"]["evidence"])

    def test_without_coefficients_or_sysfs_it_still_reports(self):
        dump = "\n".join(line for line in CODEC_DUMP.splitlines() if "Coeff" not in line)
        found = self.by_id(codec.codec_checks(self.host(dump, sysfs=False)))
        self.assertEqual(found["codec.adc_cfg"]["status"], "skip")
        self.assertEqual(found["codec.ssid"]["value"], "0x106b5200")  # from the dump itself

    def test_a_qemu_codec_skips_the_cirrus_checks(self):
        dump = "Codec: Generic\nVendor Id: 0x1af41022\nSubsystem Id: 0x1af40021\n"
        found = self.by_id(codec.codec_checks(self.host(dump, sysfs=False)))
        self.assertEqual(found["codec.adc_cfg"]["status"], "skip")
        self.assertEqual(found["codec.fixup"]["status"], "pass")  # the line is reported, but no warning

    def test_the_dumps_are_saved_with_the_report(self):
        saved = {}

        class Saver:
            def write_text(self, name, text):
                saved[name] = text

        codec.codec_checks(self.host(), Saver())
        self.assertIn("Coeff 0x02: 0x000a", saved["codec0.txt"])


class Live(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_verb_words(self):
        self.assertEqual(codec.verb(0x11, 0x5, 0x02), 0x11050002)
        self.assertEqual(codec.verb(0x11, 0xC00, 0), 0x110C0000)
        self.assertEqual(codec.verb(0x11, 0x4, 0x102A), 0x1104102A)
        self.assertEqual(codec.verb(0x0D, 0x707, 0x24), 0x0D070724)  # SET_PIN_WIDGET_CONTROL IN|VREF_80

    def test_hwdep_reads_and_writes_the_coefficient(self):
        sent = []
        state = {"index": 0, "coef": {2: 0x000A}}

        def ioctl(handle, request, buffer, mutate):
            self.assertEqual(request, codec.HDA_IOCTL_VERB_WRITE)
            word, _ = struct.unpack("II", bytes(buffer))
            sent.append(word)
            command = (word >> 16) & 0xF
            if (word >> 8) & 0xFFF == 0xC00:
                result = state["coef"].get(state["index"], 0)
            elif command == 0x5:
                state["index"], result = word & 0xFFFF, 0
            elif command == 0x4:
                state["coef"][state["index"]], result = word & 0xFFFF, 0
            else:
                result = 0
            buffer[:] = struct.pack("II", word, result)
            return 0

        class Handle:
            def close(self):
                pass

        device = codec.Hwdep("/dev/null", opener=lambda *_args, **_kw: Handle(), ioctl=ioctl)
        self.assertEqual(device.read_coef(0x11, 2), 0x000A)
        self.assertEqual(device.write_coef(0x11, 2, 0x102A), 0x102A)
        self.assertIn(0x1104102A, sent)

    def test_without_the_cable_kernel_live_changes_skip(self):
        host = FakeHost(self.root)
        self.assertEqual(codec.reconfigure(host, "mbp81")["status"], "skip")
        self.assertEqual(codec.set_coefficient(host, 0x102A)["status"], "skip")

    def test_reconfigure_writes_the_model_and_triggers(self):
        host = FakeHost(self.root)
        host.made(f"{codec.HWDEP_SYSFS}/reconfig", "")
        host.made(f"{codec.HWDEP_SYSFS}/modelname", "\n")
        result = codec.reconfigure(host, "mbp81")
        self.assertEqual(result["status"], "pass")
        self.assertEqual(host.path(f"{codec.HWDEP_SYSFS}/modelname").read_text(), "mbp81")
        self.assertEqual(host.path(f"{codec.HWDEP_SYSFS}/reconfig").read_text(), "1")
        self.assertEqual(codec.reconfigure(host, "anything; rm -rf /")["status"], "fail")


if __name__ == "__main__":
    unittest.main()


class LiveRun(unittest.TestCase):
    def test_codec_model_switch_end_to_end(self):
        import io
        import json
        from contextlib import redirect_stdout

        from mind import diag, diag_sound

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        host = FakeHost(root, programs={"systemctl", "dmesg"}, active={diag_sound.VOICE},
                        handlers={"dmesg": lambda argv: Result(0, V5_DMESG),
                                  "being-audio-init": lambda argv: Result(0, "done")})
        host.made("/proc/asound/card0/codec#0", CODEC_DUMP)
        host.made(f"{codec.HWDEP_SYSFS}/reconfig", "")
        host.made(f"{codec.HWDEP_SYSFS}/modelname", "\n")
        host.made(str(diag.AUDIO_INIT), "#!/bin/sh\n")
        with redirect_stdout(io.StringIO()):
            diag.main(["codec", "--model", "mbp81", "--out", str(root / "out")], host=host)
        data = json.loads((root / "out" / "diag.json").read_text())
        ids = {item["id"]: item["status"] for item in data["checks"]}
        self.assertEqual(ids["codec.reconfig"], "pass")
        self.assertEqual(ids["codec.mixer_reset"], "pass")
        self.assertIn("codec.adc_cfg.after", ids)
        self.assertIn(diag_sound.VOICE, host.active)
        self.assertEqual(host.path(f"{codec.HWDEP_SYSFS}/modelname").read_text(), "mbp81")

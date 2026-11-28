"""The HDA codec as the kernel set it up, and live experiments on it.

Read-only by default: the codec dump in /proc/asound/card0/codec#N, the
codec's IDs in sysfs, the "picked fixup" line in the kernel log and the
snd_hda_intel module parameters. On the MacBookPro9,2 (Cirrus Logic CS4206,
codec SSID 106b:5200) the facts that matter for the microphone are:

- which fixup the driver picked: with no entry for 5200 it falls through to
  the vendor-wide Apple quirk (printed as SSID 106b:0000);
- the ADC configuration coefficient 0x02 on vendor node 0x11: 0x000a is the
  generic default (differential), 0x102a what the mbp81 fixup writes;
- the internal microphone pin 0x0d: its pin default and VREF;
- GPIO lines 1 and 3 (headphone and speaker amplifiers).

Live experiments need the cable build's kernel (CONFIG_SND_HDA_HWDEP and
CONFIG_SND_HDA_RECONFIG) and root: a new fixup through
/sys/class/sound/hwC0D0/{modelname,reconfig}, or raw verbs through the
hwdep device's HDA_IOCTL_VERB_WRITE. Without them each reports skip. Raw
verbs bypass the driver's cache, so a codec re-initialisation can undo them.

Measured on the MacBookPro9,2 with kernel 6.18: a reconfig does not pick a
fixup again (the driver picks one only when the codec is created), so
`--model` is checked by its effect, a new "picked fixup" line or a changed
coefficient, and reports skip when nothing changed. Only the boot-time
snd_hda_intel.model= applies a fixup; `--coef 0x102a` (or 0x100a) is the live
stand-in. There is one microphone, on the right channel: mbp81's 0x102a
copies it to the left (bit 12) in single-ended mode (bit 5, 5.8 dB less
level, no better signal-to-noise), so the generic Apple fixup is kept.
"""

import re
import struct

from .diag_report import check

APPLE_SSID = 0x106B5200
DEFAULT_ADC_CFG = 0x000A
MBP81_ADC_CFG = 0x102A
VENDOR_NODE = 0x11
ADC_CFG_INDEX = 0x02
MIC_PIN = 0x0D
ADC_NODE = 0x06
DAC_NODES = (0x02, 0x03)
SPEAKER_PINS = (0x0A, 0x0B)
# Fixups the CS420x driver knows by name (sound/hda/codecs/cirrus/cs420x.c).
MODELS = ("apple", "mbp81", "mbp101", "mbp53", "mbp55", "imac27", "imac27_122", "mba42", "mba6")
VREF_CODES = {"hiz": 0x0, "50": 0x1, "grd": 0x2, "80": 0x4, "100": 0x5}
PIN_IN = 0x20
HDA_IOCTL_PVERSION = 0x80044810
HDA_IOCTL_VERB_WRITE = 0xC0084811
HWDEP = "/dev/snd/hwC0D0"
HWDEP_SYSFS = "/sys/class/sound/hwC0D0"
SYSFS_CODEC = "/sys/bus/hdaudio/devices/hdaudioC0D0"
DUMP_COEF = "/sys/module/snd_hda_codec/parameters/dump_coef"
INTEL_PARAMS = "/sys/module/snd_hda_intel/parameters"

_NODE = re.compile(r"^Node (0x[0-9a-fA-F]+) \[([^\]]+)\] wcaps (0x[0-9a-fA-F]+):\s*(.*)$")
_TOP = re.compile(r"^(Codec|Address|Vendor Id|Subsystem Id|Revision Id|AFG Function Id|MFG Function Id): (.*)$")
_PIN_DEFAULT = re.compile(r"^\s+Pin Default (0x[0-9a-fA-F]+): (.*)$")
_PIN_CTLS = re.compile(r"^\s+Pin-ctls: (0x[0-9a-fA-F]+):?\s*(.*)$")
_COEFF = re.compile(r"^\s+Coeff (0x[0-9a-fA-F]+): (0x[0-9a-fA-F]+)\s*$")
_CONNECTION = re.compile(r"^\s+Connection: (\d+)\s*$")
_RATES = re.compile(r"^\s+rates \[(0x[0-9a-fA-F]+)\]:\s*(.*)$")
_BITS = re.compile(r"^\s+bits \[(0x[0-9a-fA-F]+)\]:\s*(.*)$")
_GPIO = re.compile(r"^GPIO: io=(\d+), o=(\d+), i=(\d+)")
_GPIO_LINE = re.compile(r"^\s+IO\[(\d+)\]: (.*)$")
_FIXUP = re.compile(r"(\w+): picked fixup (.*?)\s*(?:\(([^)]*)\)|for ([\w ]+?) ([0-9a-fA-F]{4}:[0-9a-fA-F]{4}))\s*$")
_ALSACTL = re.compile(r"HDA:([0-9a-fA-F]{8}),([0-9a-fA-F]{8}),([0-9a-fA-F]{8})")


def hex_int(text):
    try:
        return int(str(text).strip(), 16)
    except (TypeError, ValueError):
        return None


def parse_codec(text):
    """A codec dump as {codec, vendor_id, subsystem_id, revision_id, gpio, nodes}.

    Each node: {type, wcaps, caps, pin_default, pin_default_text, pin_ctls,
    pin_ctls_text, connections: [(nid, selected)], coeffs: {index: value},
    rates: [Hz], bits: [n]}. Unknown lines are kept nowhere: only what is read
    here is reported.
    """
    codec = {"codec": None, "vendor_id": None, "subsystem_id": None, "revision_id": None,
             "gpio": None, "nodes": {}}
    node = None
    pending_connections = 0
    in_pcm = False
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if pending_connections:
            pending_connections = 0
            if node is not None:
                node["connections"] = [(hex_int(token.rstrip("*")), token.endswith("*"))
                                       for token in line.split() if hex_int(token.rstrip("*")) is not None]
            continue
        match = _NODE.match(line)
        if match:
            nid = hex_int(match[1])
            node = {"type": match[2], "wcaps": hex_int(match[3]), "caps": match[4].strip(), "pin_default": None,
                    "pin_default_text": "", "pin_ctls": None, "pin_ctls_text": "", "connections": [],
                    "coeffs": {}, "rates": [], "bits": []}
            codec["nodes"][nid] = node
            in_pcm = False
            continue
        if node is None:
            match = _TOP.match(line)
            if match:
                key = match[1].lower().replace(" ", "_")
                text_field = key in ("codec", "afg_function_id", "mfg_function_id", "address")
                codec[key] = match[2].strip() if text_field else hex_int(match[2])
                continue
            match = _GPIO.match(line)
            if match:
                codec["gpio"] = {"io": int(match[1]), "o": int(match[2]), "i": int(match[3]), "lines": {}}
                continue
            match = _GPIO_LINE.match(line)
            if match and codec["gpio"] is not None:
                fields = dict(part.strip().split("=", 1) for part in match[2].split(",") if "=" in part)
                codec["gpio"]["lines"][int(match[1])] = {key: int(value) if value.isdigit() else value
                                                         for key, value in fields.items()}
            continue
        if line.strip() == "PCM:":
            in_pcm = True
            continue
        for pattern, action in ((_PIN_DEFAULT, "pin_default"), (_PIN_CTLS, "pin_ctls")):
            match = pattern.match(line)
            if match:
                node[action] = hex_int(match[1])
                node[action + "_text"] = match[2].strip()
                break
        else:
            match = _COEFF.match(line)
            if match:
                node["coeffs"][hex_int(match[1])] = hex_int(match[2])
                continue
            match = _CONNECTION.match(line)
            if match:
                pending_connections = int(match[1]) > 0
                continue
            match = _RATES.match(line)
            if match and in_pcm:
                node["rates"] = [int(word) for word in match[2].split() if word.isdigit()]
                continue
            match = _BITS.match(line)
            if match and in_pcm:
                node["bits"] = [int(word) for word in match[2].split() if word.isdigit()]
    return codec


def picked_fixup(log_text):
    """{chip, name, reason, match_type, ssid} from the kernel's "picked fixup" line, or None.

    The fixup name is blank unless CONFIG_SND_DEBUG is on. A blank name with
    SSID 106b:0000 is the vendor-wide Apple entry (CS420X_APPLE)."""
    for line in reversed((log_text or "").splitlines()):
        match = _FIXUP.search(line)
        if match:
            name = match[2].strip()
            ssid = match[5].lower() if match[5] else None
            meaning = name or ("vendor-wide Apple quirk (CS420X_APPLE)" if ssid == "106b:0000" else "unnamed")
            return {"chip": match[1], "name": name, "reason": match[3] or None,
                    "match_type": (match[4] or "").strip() or None, "ssid": ssid, "meaning": meaning}
    return None


def alsactl_ids(log_text):
    """[(vendor, subsystem, revision)] from alsactl's "Found hardware" line."""
    return [(int(a, 16), int(b, 16), int(c, 16)) for a, b, c in _ALSACTL.findall(log_text or "")]


def describe_vref(text):
    match = re.search(r"VREF_(\w+)", text or "")
    return match[1] if match else None


# Reading --------------------------------------------------------------------

def sysfs_ids(host):
    values = {}
    for name in ("vendor_id", "subsystem_id", "revision_id", "chip_name"):
        text = host.read(f"{SYSFS_CODEC}/{name}")
        if text is not None:
            values[name] = text.strip()
    return values


def intel_parameters(host):
    values = {}
    for path in host.glob(INTEL_PARAMS, "*"):
        text = host.read(f"{INTEL_PARAMS}/{path.name}", 4096)
        if text is not None and path.name in ("model", "position_fix", "bdl_pos_adj", "probe_mask", "enable_msi",
                                              "power_save", "single_cmd", "snoop", "patch", "dmic_detect"):
            values[path.name] = text.strip()
    return values


def read_dump(host, card=0, codec=0, *, with_coeffs=True):
    """The codec dump text. As root it turns dump_coef on for the read and back after."""
    path = f"/proc/asound/card{card}/codec#{codec}"
    old = host.read(DUMP_COEF) if with_coeffs and host.is_root() else None
    if old is not None and old.strip() != "1":
        host.write(DUMP_COEF, "1")
    try:
        return host.read(path)
    finally:
        if old is not None and old.strip() != "1":
            host.write(DUMP_COEF, old.strip())


def kernel_log(host):
    result = host.run(["dmesg"], timeout=10)
    if result is not None and result.code == 0 and result.out:
        return result.out
    result = host.run(["journalctl", "-k", "-b", "--no-pager", "-q", "-o", "cat"], timeout=20)
    return result.out if result is not None and result.code == 0 else ""


def live_available(host):
    """(hwdep, reconfig): whether raw verbs and live fixup changes can work here."""
    return host.exists(HWDEP), host.exists(f"{HWDEP_SYSFS}/reconfig")


# The checks -------------------------------------------------------------------

def codec_checks(host, report=None):
    """Every read-only codec check. Saves codec0.txt and codec3.txt when a report is given."""
    if not host.exists("/proc/asound"):
        return [check("codec.present", "codec", "skip", None, "no /proc/asound: no sound card on this machine")]
    dump = read_dump(host, 0, 0)
    if report is not None:
        for number in (0, 3):
            text = dump if number == 0 else read_dump(host, 0, number, with_coeffs=False)
            if text:
                report.write_text(f"codec{number}.txt", text)
    if not dump:
        return [check("codec.present", "codec", "skip", None, "no codec#0 on card 0 (QEMU or another machine)")]
    codec = parse_codec(dump)
    log = kernel_log(host)
    ids = sysfs_ids(host)
    ssid = hex_int(ids.get("subsystem_id")) or codec["subsystem_id"]
    checks = [check("codec.present", "codec", "pass", codec["codec"],
                    f"{codec['codec']}, vendor {_hex(codec['vendor_id'], 8)}")]
    cirrus = "cirrus" in (codec["codec"] or "").casefold() or codec["vendor_id"] == 0x10134206
    alsactl = alsactl_ids(log)
    evidence = f"codec SSID {_hex(ssid, 8)}" + (" (sysfs)" if ids.get("subsystem_id") else " (codec dump)")
    if alsactl:
        evidence += f"; alsactl saw {', '.join(f'{_hex(v, 8)},{_hex(s, 8)}' for v, s, _ in alsactl)}"
    checks.append(check("codec.ssid", "codec", "pass" if ssid else "warn", _hex(ssid, 8), evidence,
                        "" if ssid else "The codec reported no subsystem ID."))
    fixup = picked_fixup(log)
    if fixup is None:
        checks.append(check("codec.fixup", "codec", "skip", None, "no 'picked fixup' line in the kernel log"))
    else:
        note = ""
        if cirrus and ssid == APPLE_SSID and not fixup["name"] and fixup["ssid"] == "106b:0000":
            # Measured: mbp81 adds no microphone on this Mac, so the generic Apple fixup is right.
            note = "; this Mac (106b:5200) has no entry of its own and the generic Apple fixup is the one to keep"
        checks.append(check("codec.fixup", "codec", "pass", fixup,
                            f"{fixup['chip']}: {fixup['meaning']}"
                            + (f" ({fixup['reason']})" if fixup["reason"] else "")
                            + (f", matched {fixup['match_type']} {fixup['ssid']}" if fixup["ssid"] else "") + note))
    params = intel_parameters(host)
    model = params.get("model", "")
    checks.append(check("codec.params", "codec", "pass", params,
                        ", ".join(f"{key}={value}" for key, value in sorted(params.items())) or "none readable"))
    checks.extend(adc_checks(codec, ssid, cirrus, model))
    hwdep, reconfig = live_available(host)
    checks.append(check("codec.live", "codec", "pass" if hwdep and reconfig else "skip",
                        {"hwdep": hwdep, "reconfig": reconfig},
                        "live codec changes are possible (cable kernel)" if hwdep and reconfig else
                        "no hwdep/reconfig in this kernel: live codec experiments need the cable build"))
    bdl = len(re.findall(r"bdl_pos_adj", log))
    checks.append(check("codec.irq_timing", "codec", "pass", bdl,
                        f"'bdl_pos_adj' IRQ timing notes in the kernel log: {bdl}"))
    return checks


def _hex(value, width=4):
    return None if value is None else f"0x{value:0{width}x}"


def adc_checks(codec, ssid, cirrus, model=""):
    nodes = codec["nodes"]
    checks = []
    vendor = nodes.get(VENDOR_NODE)
    coeff = vendor["coeffs"].get(ADC_CFG_INDEX) if vendor else None
    if not cirrus:
        checks.append(check("codec.adc_cfg", "codec", "skip", None, "not a Cirrus Logic codec"))
    elif coeff is None:
        checks.append(check("codec.adc_cfg", "codec", "skip", None,
                            "node 0x11 coefficients are not in the dump (needs root for dump_coef)"))
    else:
        meaning = {DEFAULT_ADC_CFG: "generic default, differential mic input",
                   MBP81_ADC_CFG: "mbp81: the right channel copied to the left, single ended",
                   0x100A: "the right channel copied to the left, differential",
                   0x002A: "single ended"}.get(coeff, "unrecognised")
        note = ""
        if coeff == DEFAULT_ADC_CFG and ssid == APPLE_SSID:
            note = "; on this Mac the one microphone is on the right channel, so the left reads nothing"
        elif coeff == MBP81_ADC_CFG and ssid == APPLE_SSID:
            note = "; measured on this Mac: 5.8 dB less level than 0x000a for the same signal-to-noise"
        checks.append(check("codec.adc_cfg", "codec", "pass", _hex(coeff),
                            f"node 0x11 Coeff 0x02 = {_hex(coeff)} ({meaning}); model param '{model or '-'}'"
                            + note))
    pin = nodes.get(MIC_PIN)
    if pin is None:
        checks.append(check("codec.mic_pin", "codec", "skip", None, "no pin 0x0d in this codec"))
    else:
        vref = describe_vref(pin["pin_ctls_text"])
        enabled = pin["pin_ctls"] is not None and bool(pin["pin_ctls"] & PIN_IN)
        checks.append(check("codec.mic_pin", "codec", "pass" if enabled else "warn",
                            {"pin_default": _hex(pin["pin_default"], 8), "pin_ctls": _hex(pin["pin_ctls"], 2),
                             "vref": vref},
                            f"pin 0x0d {pin['pin_default_text'] or '?'}; Pin-ctls {_hex(pin['pin_ctls'], 2)} "
                            f"{pin['pin_ctls_text']}",
                            "" if enabled else "The internal mic pin's input is switched off."))
    adc = nodes.get(ADC_NODE)
    if adc is not None:
        connections = " ".join(f"{_hex(nid, 2)}{'*' if chosen else ''}" for nid, chosen in adc["connections"])
        chosen = [nid for nid, selected in adc["connections"] if selected]
        checks.append(check("codec.adc_connection", "codec", "pass", connections,
                            f"ADC 0x06 connects {connections or 'nothing'}"
                            + ("; selected the mic pin" if MIC_PIN in chosen else "")))
    gpio = codec.get("gpio")
    if gpio:
        lines = {number: gpio["lines"].get(number, {}) for number in (1, 3)}
        checks.append(check("codec.gpio", "codec", "pass", lines,
                            "; ".join(f"IO[{number}] enable={line.get('enable')} dir={line.get('dir')} "
                                      f"data={line.get('data')}" for number, line in lines.items())))
    rates = {_hex(nid, 2): nodes[nid]["rates"] for nid in DAC_NODES if nid in nodes}
    if rates:
        checks.append(check("codec.dac_rates", "codec", "pass", rates,
                            "; ".join(f"DAC {nid}: {' '.join(map(str, values)) or '?'} Hz"
                                      for nid, values in rates.items())))
    speakers = {_hex(nid, 2): nodes[nid]["pin_default_text"] for nid in SPEAKER_PINS if nid in nodes}
    if speakers:
        checks.append(check("codec.speaker_pins", "codec", "pass", speakers,
                            "; ".join(f"pin {nid}: {text}" for nid, text in speakers.items())))
    return checks


# Live experiments -------------------------------------------------------------

def verb(nid, command, parameter):
    """A verb word for HDA_IOCTL_VERB_WRITE: the node in the top byte (the kernel
    reads nid = word >> 24, verb = (word >> 8) & 0xffff, parameter = word & 0xff).
    12-bit commands take 8 bits of parameter, 4-bit ones (coefficients) 16."""
    if command > 0xF:
        return (nid << 24) | (command << 8) | (parameter & 0xFF)
    return (nid << 24) | (command << 16) | (parameter & 0xFFFF)


class Hwdep:
    """Raw verbs through /dev/snd/hwC0D0 (HDA_IOCTL_VERB_WRITE)."""

    def __init__(self, path=HWDEP, opener=open, ioctl=None):
        import fcntl

        self.ioctl = ioctl or fcntl.ioctl
        self.handle = opener(path, "rb+", buffering=0)

    def close(self):
        self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, word):
        buffer = bytearray(struct.pack("II", word, 0))
        self.ioctl(self.handle, HDA_IOCTL_VERB_WRITE, buffer, True)
        return struct.unpack("II", bytes(buffer))[1]

    def read_coef(self, nid, index):
        self.send(verb(nid, 0x5, index))            # SET_COEF_INDEX
        return self.send(verb(nid, 0xC00, 0)) & 0xFFFF  # GET_PROC_COEF

    def write_coef(self, nid, index, value):
        self.send(verb(nid, 0x5, index))
        self.send(verb(nid, 0x4, value))            # SET_PROC_COEF
        return self.read_coef(nid, index)

    def set_vref(self, nid, code):
        self.send(verb(nid, 0x707, PIN_IN | code))  # SET_PIN_WIDGET_CONTROL
        return self.send(verb(nid, 0xF07, 0))       # GET_PIN_WIDGET_CONTROL


FIXUP_LINE = re.compile(r"picked fixup")
# What each fixup writes to node 0x11 coefficient 0x02 on the CS4206, where it is known.
EXPECTED_ADC_CFG = {"apple": DEFAULT_ADC_CFG, "mbp81": MBP81_ADC_CFG}


def current_coefficient(host, *, hwdep_factory=None):
    """Node 0x11 coefficient 0x02 as the codec holds it now, or None.

    By a raw verb when the hwdep device is there (exact, after any re-init),
    else from the codec dump with dump_coef (root)."""
    hwdep, _ = live_available(host)
    if hwdep:
        try:
            with (hwdep_factory or Hwdep)(str(host.path(HWDEP))) as device:
                return device.read_coef(VENDOR_NODE, ADC_CFG_INDEX)
        except OSError:
            pass
    dump = read_dump(host, 0, 0)
    vendor = parse_codec(dump)["nodes"].get(VENDOR_NODE) if dump else None
    return vendor["coeffs"].get(ADC_CFG_INDEX) if vendor else None


def fixup_lines(log_text):
    return [line for line in (log_text or "").splitlines() if FIXUP_LINE.search(line)]


def reconfigure(host, model, *, hwdep_factory=None):
    """Ask the driver for another fixup now, and check by its effect whether it took.

    Needs root, the voice stopped and no open PCM. On kernel 6.18 a reconfig
    rebuilds the codec but keeps the fixup it picked when the codec was
    created: then no new "picked fixup" line appears and the coefficient
    stays, and this says skip (the experiment could not be done live), not
    pass. The value's `written` says the reconfig ran: the mixer then came
    back at the driver's defaults, and the caller puts the levels back."""
    if model not in MODELS:
        return check("codec.reconfig", "codec", "fail", model, f"unknown model {model!r}",
                     f"Use one of: {', '.join(MODELS)}.")
    hwdep, reconfig = live_available(host)
    if not reconfig:
        return check("codec.reconfig", "codec", "skip", model,
                     "this kernel has no HDA reconfig (CONFIG_SND_HDA_RECONFIG): use the cable build")
    before = (host.read(f"{HWDEP_SYSFS}/modelname") or "").strip()
    coef_before = current_coefficient(host, hwdep_factory=hwdep_factory)
    lines_before = len(fixup_lines(kernel_log(host)))
    if not host.write(f"{HWDEP_SYSFS}/modelname", model) or not host.write(f"{HWDEP_SYSFS}/reconfig", "1"):
        return check("codec.reconfig", "codec", "fail", {"model": model, "before": before, "written": False},
                     f"writing modelname/reconfig failed (was {before!r})",
                     "Something still has the sound card open (the voice, aplay or arecord).")
    after = (host.read(f"{HWDEP_SYSFS}/modelname") or "").strip()
    coef_after = current_coefficient(host, hwdep_factory=hwdep_factory)
    log = kernel_log(host)
    new_lines = fixup_lines(log)[lines_before:]
    picked = picked_fixup("\n".join(new_lines)) if new_lines else None
    value = {"model": model, "before": before, "after": after, "written": True, "coef_before": _hex(coef_before),
             "coef_after": _hex(coef_after), "picked": picked}
    expected = EXPECTED_ADC_CFG.get(model)
    stand_in = ("A fixup is applied only at boot (snd_hda_intel.model=" + model + "); `pip-diag codec --coef "
                "0x102a` (or 0x100a) writes the coefficient live instead.")
    if picked is not None:
        if picked["name"] and picked["name"] != model:
            return check("codec.reconfig", "codec", "fail", value,
                         f"asked for {model}, but the kernel picked {picked['name']}",
                         "The driver does not know this model name for this codec.")
        if expected is not None and coef_after is not None and coef_after != expected:
            return check("codec.reconfig", "codec", "fail", value,
                         f"the kernel picked a fixup again, but coef 0x02 is {_hex(coef_after)}, "
                         f"not {model}'s {_hex(expected)}", "The fixup did not apply its codec setup.")
        return check("codec.reconfig", "codec", "pass", value,
                     f"codec reconfigured to {model}: the kernel picked {picked['meaning']}; coef 0x02 "
                     f"{_hex(coef_before) or '?'} -> {_hex(coef_after) or '?'}")
    if coef_before is not None and coef_after is not None and coef_after != coef_before:
        if expected is not None and coef_after != expected:
            return check("codec.reconfig", "codec", "fail", value,
                         f"coef 0x02 changed {_hex(coef_before)} -> {_hex(coef_after)}, not to {model}'s "
                         f"{_hex(expected)}", "The fixup did not apply its codec setup.")
        return check("codec.reconfig", "codec", "pass", value,
                     f"codec reconfigured to {model}: coef 0x02 {_hex(coef_before)} -> {_hex(coef_after)}")
    if coef_before is None or coef_after is None:
        return check("codec.reconfig", "codec", "skip", value,
                     f"the codec was reconfigured with model {after or model}, but there is no new 'picked fixup' "
                     "line and coef 0x02 could not be read, so whether the fixup changed is unknown", stand_in)
    return check("codec.reconfig", "codec", "skip", value,
                 f"the codec was reconfigured with model {after or model}, but the kernel kept its fixup: no new "
                 f"'picked fixup' line and coef 0x02 still {_hex(coef_after)} (a fixup is picked only when the "
                 "codec is created)", stand_in)


def set_coefficient(host, value, *, hwdep_factory=None):
    """Write node 0x11 coefficient 0x02 by a raw verb and read it back."""
    hwdep, _ = live_available(host)
    if not hwdep:
        return check("codec.coef", "codec", "skip", _hex(value),
                     "no /dev/snd/hwC0D0 (CONFIG_SND_HDA_HWDEP): use the cable build")
    try:
        with (hwdep_factory or Hwdep)(str(host.path(HWDEP))) as device:
            before = device.read_coef(VENDOR_NODE, ADC_CFG_INDEX)
            after = device.write_coef(VENDOR_NODE, ADC_CFG_INDEX, value)
    except OSError as error:
        return check("codec.coef", "codec", "fail", _hex(value), f"the hwdep verb failed: {error}")
    status = "pass" if after == value else "fail"
    return check("codec.coef", "codec", status, {"before": _hex(before), "after": _hex(after)},
                 f"node 0x11 Coeff 0x02: {_hex(before)} -> {_hex(after)} (raw verb; put back when the run ends)",
                 "" if status == "pass" else "The codec did not keep the new value.")


# Putting the codec back ----------------------------------------------------------

def experiment_state(host, *, hwdep_factory=None):
    """What a live experiment changes, as it is now: the model name the next reconfig
    uses (None without the sysfs file) and node 0x11 coefficient 0x02 (None if unreadable)."""
    model = host.read(f"{HWDEP_SYSFS}/modelname")
    return {"modelname": None if model is None else model.strip(),
            "coef": current_coefficient(host, hwdep_factory=hwdep_factory)}


def put_back(host, saved, *, hwdep_factory=None):
    """Undo `codec --model` and `codec --coef`: the model name written back and the codec
    reconfigured to it, then coefficient 0x02 written back by a raw verb if it still differs.

    Needs the voice stopped. Never raises. Returns (what it did, whether it reconfigured
    the codec: a reconfig resets the mixer, so the caller puts the levels back after)."""
    done, reconfigured = [], False
    if not isinstance(saved, dict):
        return done, reconfigured
    try:
        wanted = saved.get("modelname")
        now = host.read(f"{HWDEP_SYSFS}/modelname")
        if isinstance(wanted, str) and now is not None and now.strip() != wanted:
            # An empty name ("\n": a write of nothing never reaches sysfs) means the boot's own choice.
            ok = host.write(f"{HWDEP_SYSFS}/modelname", wanted or "\n")
            ok = ok and host.write(f"{HWDEP_SYSFS}/reconfig", "1")
            reconfigured = ok
            shown = wanted or "the boot's choice"
            done.append(f"put the codec model back to {shown} and reconfigured it" if ok else
                        f"could not put the codec model back to {shown} (it stays {now.strip()!r} until the reboot)")
        coef = saved.get("coef")
        if isinstance(coef, int):
            now = current_coefficient(host, hwdep_factory=hwdep_factory)
            if now is not None and now != coef:
                after = None
                if live_available(host)[0]:
                    try:
                        with (hwdep_factory or Hwdep)(str(host.path(HWDEP))) as device:
                            after = device.write_coef(VENDOR_NODE, ADC_CFG_INDEX, coef)
                    except OSError:
                        after = None
                done.append(f"put the codec coef 0x02 back to {_hex(coef)}" if after == coef else
                            f"could not put the codec coef 0x02 back to {_hex(coef)} (it stays {_hex(now)} "
                            "until the reboot)")
    except Exception as error:  # restore must go on with the mixer and the voice
        done.append(f"could not put the codec back: {type(error).__name__}: {error}")
    return done, reconfigured

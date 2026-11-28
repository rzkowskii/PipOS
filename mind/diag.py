"""pip-diag: measure Pip's hearing, voice, commands and body, and say what is wrong.

    pip-diag quick       about a minute, automatic
    pip-diag full        10-15 minutes, automatic (speaker, loopback, voices, engines)
    pip-diag voice       interactive: the owner speaks, rates the voices, tries the wake phrase
    pip-diag commands    the command phrases through the mind's own routing
    pip-diag workload    addressed route counts and CPU stalls in the reflex organs
    pip-diag continuity  read the life record, archive health and live cortex slots
    pip-diag mic         room floor per channel and the Mic Boost x Capture sweep
    pip-diag speaker     the test-signal battery through plughw:0,0 and hw:0,0, and the loopback
    pip-diag echo-trace  explicitly consented RAM-only host-scheduled echo trace
    pip-diag native-echo-trace  explicitly consented linked-ALSA duplex evidence trace
    pip-diag tts         every voice: speed (at the voice's CPU weight), and words decoded before
                         and after the speaker
    pip-diag codec       the codec dump; --coef writes the mic ADC coefficient live (cable build);
                         --model asks for another fixup and says skip when the kernel keeps its own
    pip-diag hear FILE   play a WAV to the running voice (or --phrase TEXT, rendered here) over
                         this laptop's room floor; --level realistic or both: also at a person's level
    pip-diag engines     the speech engines' speed check and the on-CPU speech smoke;
                         --record-smoke keeps its verdict for the voice's automatic choice
                         (on a Try boot only until the reboot: /mind is in RAM)
    pip-diag restore     put the voice and mixer back after an interrupted run
    pip-diag report      show the latest report

Results go to <PIPLOGS boot folder>/diag-<UTC>/ (or /run/being-diag/<UTC>/, or --out):
diag.json, diag.txt (the top problems first and a plain-English verdict), and
the evidence files. `--json` prints one JSON line per check instead.

Exit codes: 0 nothing failed, 1 something failed, 2 the run could not happen.
Sections that open the sound card stop the voice first and always restart it,
running in the transient unit pip-diag-audio at the voice's CPU weight.
Owner speech is kept only with --yes-keep-recordings or a yes when asked.
"""

import argparse
import json
import os
import signal
import sys
from pathlib import Path

from . import diag_codec, diag_sound, diag_speech, diag_system
from .diag_report import (RUN_DIR, Interrupted, Report, check, exit_code, guarded, latest_report, output_folder,
                          utc_stamp)
from .diag_system import Host, Journal

AUDIO = {"full", "voice", "mic", "speaker", "echo-trace", "native-echo-trace", "tts", "codec", "hear"}
ROOT_ONLY = AUDIO | {"restore"}
UNIT_NAME = "pip-diag-audio"
# The voice organ's own CPU weight (being-voice.service): speech timings taken
# at the default 100 would overstate the real-time factor beside the cortex.
VOICE_CPU_WEIGHT = 1000
WRAPPER = Path("/usr/sbin/pip-diag")
LOCK = RUN_DIR / "lock"
# Present while a run is going: for the boot summary and the status overlay to say diagnostics were on.
ACTIVE = RUN_DIR / "active.json"
COMMANDS = {
    "quick": "about a minute, automatic",
    "full": "10-15 minutes, automatic",
    "voice": "interactive, with the owner speaking and listening",
    "commands": "command phrases through the mind's routing",
    "workload": "addressed route counts and CPU stalls in the reflex organs",
    "continuity": "read the life record, archive health and live cortex slots",
    "mic": "room floor per channel and the gain sweep",
    "speaker": "the test-signal battery and the loopback",
    "echo-trace": "one explicitly consented RAM-only echo reference trace",
    "native-echo-trace": "one explicitly consented linked-ALSA duplex evidence trace",
    "tts": "every voice, scored before and after the speaker",
    "codec": "the codec as set up; --model/--coef change it live",
    "hear": "play a WAV (or --phrase) to the running voice",
    "engines": "the speech engines' speed check and the speech smoke on this CPU",
    "restore": "put the voice and mixer back",
    "report": "show the latest report",
}


class CannotRun(Exception):
    """The run could not happen at all (exit code 2)."""


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="one JSON line per check on stdout")
    common.add_argument("--now", type=float, metavar="UNIX", help="the Mac's time (date -u +%%s), for clock drift")
    common.add_argument("--out", metavar="DIR", help="write the report here instead of PIPLOGS")
    common.add_argument("--yes-keep-recordings", action="store_true",
                        help="keep owner speech recordings and transcripts (default: RAM only)")
    common.add_argument("--stimulus-level", type=float, default=-20.0, metavar="DBFS",
                        help="test signal level, -40 to -3 dBFS (default -20)")
    common.add_argument("--no-unit", action="store_true", help="do not run audio work under systemd-run")
    top = argparse.ArgumentParser(prog="pip-diag", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, summary_text in COMMANDS.items():
        command = sub.add_parser(name, parents=[common], help=summary_text, description=summary_text)
        if name == "codec":
            command.add_argument("--model", choices=diag_codec.MODELS,
                                 help="ask the driver for another fixup now; checked by its effect "
                                      "(on kernel 6.18 a fixup applies only at boot, so this says skip)")
            command.add_argument("--coef", metavar="HEX", help="write node 0x11 coefficient 0x02 (0x102a or 0x000a)")
        elif name == "hear":
            command.add_argument("file", nargs="?", help="a WAV file (any rate; the louder channel is used)")
            command.add_argument("--phrase", help="render this text on Pip instead of a file")
            command.add_argument("--voice", default="slt", choices=("slt", "kal16", "piper"),
                                 help="the voice for --phrase")
            command.add_argument("--asr", choices=("pocketsphinx", "moonshine", "auto"),
                                 help="force the voice's recognizer for this run")
            command.add_argument("--expect-route", help="the route the mind should take (e.g. workshop.open)")
            command.add_argument("--patience", type=float, default=1.0, metavar="N",
                                 help="multiply every wait by N, for an emulated CPU (default 1)")
            command.add_argument("--level", choices=diag_speech.LEVEL_CHOICES, default="clean",
                                 help="clean (default): as rendered, over a -64 dBFS room floor; realistic: "
                                      "a -30 dBFS peak over that floor, as this laptop's microphone hears a "
                                      "person; both: clean, then realistic")
        elif name == "voice":
            command.add_argument("--trials", type=int, default=10, help="wake trials per distance (default 10)")
            command.add_argument("--phrases", type=int, default=10, help="command phrases to say (10-40)")
            command.add_argument("--two-metres", action="store_true", help="also run the wake trials at 2 m")
        elif name in ("echo-trace", "native-echo-trace"):
            command.add_argument("--consent-room-audio", action="store_true",
                                 help="confirm that a short room recording may be kept privately in RAM")
        elif name == "commands":
            command.add_argument("--e2e", action="store_true",
                                 help="also say each phrase to the running voice through the diagnostic mic")
            command.add_argument("--level", choices=diag_speech.LEVEL_CHOICES, default="both",
                                 help="the level of the spoken phrases: both (default), clean or realistic "
                                      "(see `pip-diag hear --help`)")
        elif name == "engines":
            command.add_argument("--record-smoke", action="store_true",
                                 help="keep the speech smoke's verdict in /mind/audio, so the voice's automatic "
                                      "choice may use Moonshine on this build (root; on a Try boot only "
                                      "until the reboot, as /mind is in RAM)")
        elif name == "report":
            command.add_argument("folder", nargs="?", help="a diag folder (default: the latest)")
        elif name == "restore":
            command.add_argument("--quiet", action="store_true",
                                 help="say nothing on stdout (what it did goes to stderr); used as ExecStopPost")
            command.add_argument("--force", action="store_true",
                                 help="restore even while another pip-diag run holds the sound card")
    return top


def meta(host, args, folder):
    words = diag_system.cmdline(host)
    data = {"kind": "pipos-diag-v1", "command": args.command, "started_utc": utc_stamp(), "folder": str(folder),
            "boot_id": diag_system.boot_id(host) or None, "build_id": diag_system.build_id(host) or None,
            "entry": diag_system.cmdline_options(words), "uid": host.euid(),
            "keep_recordings": bool(args.yes_keep_recordings),
            "clock": {"system_utc": round(host.time()), "monotonic": round(host.monotonic(), 3),
                      "mac_utc": args.now, "drift_s": None if args.now is None else round(host.time() - args.now)}}
    return data


# The sections ---------------------------------------------------------------------

def system_section(host, report, args, *, imports=True):
    report.add(guarded("system.build", "system", diag_system.check_build, host))
    report.add(guarded("system.clock", "system", diag_system.check_clock, host, args.now))
    report.add(guarded("system.units", "system", diag_system.check_units, host))
    if imports:
        report.add(guarded("system.imports", "system", diag_system.check_imports, host))
    report.add(guarded("system.bus", "system", diag_system.check_bus, host))
    report.add(guarded("system.watchdog", "system", diag_system.check_watchdog, host))
    report.add(guarded("boot.timing", "boot", diag_system.check_boot_timing, host))


def body_section(host, report, *, full=False):
    report.add(guarded("body.thermal", "body", diag_system.check_thermal, host, 60 if full else 5, load=full))
    report.add(guarded("body.battery", "body", diag_system.check_battery, host))
    report.add(guarded("body.network", "body", diag_system.check_network, host))
    report.add(guarded("body.firewall", "body", diag_system.check_firewall, host))
    report.add(guarded("body.storage", "body", diag_system.check_storage, host, report.folder))
    report.add(guarded("body.devices", "body", diag_system.check_devices, host))


def cortex_section(host, report, *, full=False):
    prompts = diag_system.PROMPTS if full else diag_system.PROMPTS[:2]
    report.add(guarded("cortex.health", "cortex", diag_system.check_cortex, host, prompts=prompts))


def needs_root(host, report, section, what):
    if host.is_root():
        return False
    report.add([check(f"{section}.root", section, "skip", None, f"{what} needs root (run it from the Try menu "
                                                                 "or as root over the cable)")])
    return True


def sound_card(host):
    return host.exists("/proc/asound/card0") and host.which("arecord") is not None


def sphinx_or_skip(report, section="tts"):
    sphinx, why = diag_speech.open_sphinx()
    if sphinx is None:
        report.add([check(f"{section}.decoder", section, "skip", None, why)])
    return sphinx


def audio_block(host, report, args, *, mic=False, sweep=False, speaker=False, loop=False, isolation=False,
                tts=False, acoustic=False, quick_speaker=False):
    """The parts that open the card, inside one AudioSession (voice stopped, then restored)."""
    if needs_root(host, report, "mic", "Opening the sound card"):
        return
    if host.path("/run/being/mic-policy-breach").exists():
        report.add([check("mic.safety_latch", "mic", "skip", None,
                          "Active audio checks are unavailable while the microphone safety latch is active.")])
        return
    if not sound_card(host):
        report.add([check("mic.card", "mic", "skip", None, "no sound card 0 or no arecord here")])
        if tts:
            tts_section(host, report, acoustic=False)
        return
    with diag_sound.AudioSession(host) as session:
        if mic:
            report.add(guarded("mic.mixer", "mic", diag_sound.check_mixer, host, report))
            report.add(guarded("mic.floor", "mic", diag_sound.check_mic_floor, host, report,
                               keep=args.yes_keep_recordings))
        if sweep:
            report.add(guarded("mic.gain_sweep", "mic", diag_sound.check_gain_sweep, host, report, session,
                               level_dbfs=args.stimulus_level))
        # The whole battery once, shared by the speaker, loopback and isolation checks.
        files = diag_sound.speaker_stimuli(report, args.stimulus_level) if speaker or loop or isolation else None
        if speaker or quick_speaker:
            report.add(guarded("speaker.battery", "speaker", diag_sound.check_speaker_battery, host, report,
                               level_dbfs=args.stimulus_level, full=speaker, files=files))
        if speaker:
            # Small, default and large periods: tells a period-timing fault from the rest.
            report.add(guarded("speaker.timing", "speaker", diag_sound.check_speaker_timing, host, report, files,
                               level_dbfs=args.stimulus_level))
        if loop:
            report.add(guarded("loopback.level", "loopback", diag_sound.check_loopback, host, report,
                               level_dbfs=args.stimulus_level, files=files))
        if isolation:
            report.add(guarded("speaker.isolation", "speaker", diag_sound.check_speaker_isolation, host, report,
                               session, files))
        if tts:
            tts_section(host, report, acoustic=acoustic)
    report.extra["restore"] = session.restored


def echo_trace_section(host, report, args):
    """One opt-in trace, guarded by a current kernel lid reading and bus beat."""
    def authorized():
        if host.path("/run/being/mic-policy-breach").exists():
            return False
        # This is in the acquisition polling loop: a wedged probe must not
        # extend room-audio authority by several seconds.
        probe = host.run(["/usr/bin/being-brainstem", "--probe-hardware"], timeout=0.5)
        lid_open = probe is not None and probe.code == 0 and any(
            line.strip().startswith("lid:") and " says open" in line for line in probe.out.splitlines())
        try:
            beat = diag_system.read_beat(host, host.path(diag_system.bus_path(host)), 0.75)
        except OSError:
            beat = None
        return lid_open and beat is not None

    if not authorized():
        raise CannotRun("echo trace needs a confirmed open lid and a live brainstem heartbeat")
    with diag_sound.AudioSession(host) as session:
        report.add(guarded("echo.trace", "echo", diag_sound.capture_echo_trace, host, report,
                           level_dbfs=args.stimulus_level, authorized=authorized))
    report.extra["restore"] = session.restored


def native_echo_trace_section(host, report, args):
    """Native linked duplex evidence under the same live authorization envelope."""
    def authorized():
        if host.path("/run/being/mic-policy-breach").exists():
            return False
        # Bound the slowest revocation observation while the native helper is
        # holding both PCM directions. Failure to answer is failure to remain authorised.
        probe = host.run(["/usr/bin/being-brainstem", "--probe-hardware"], timeout=0.5)
        lid_open = probe is not None and probe.code == 0 and any(
            line.strip().startswith("lid:") and " says open" in line for line in probe.out.splitlines())
        try:
            beat = diag_system.read_beat(host, host.path(diag_system.bus_path(host)), 0.75)
        except OSError:
            beat = None
        return lid_open and beat is not None

    if not authorized():
        raise CannotRun("native echo trace needs a confirmed open lid and a live brainstem heartbeat")
    with diag_sound.AudioSession(host) as session:
        report.add(guarded("echo.native", "echo", diag_sound.capture_native_echo_trace,
                           host, report, session, level_dbfs=args.stimulus_level,
                           authorized=authorized))
    report.extra["restore"] = session.restored


def tts_section(host, report, *, acoustic=False, selftest=True):
    sphinx = sphinx_or_skip(report)
    try:
        report.add(guarded("tts.voices", "tts", diag_speech.check_tts, host, report, sphinx=sphinx,
                           acoustic=acoustic))
        if selftest:
            report.add(guarded("asr.selftest", "asr", diag_speech.check_asr_selftest, host, report, sphinx))
    finally:
        if sphinx is not None:
            sphinx.close()


def codec_section(host, report, args=None):
    report.add(guarded("codec.present", "codec", diag_codec.codec_checks, host, report))
    if args is None or not (getattr(args, "model", None) or getattr(args, "coef", None)):
        return
    if needs_root(host, report, "codec", "Changing the codec"):
        return
    with diag_sound.AudioSession(host) as session:
        # The model name and coef 0x02 as they are, in the marker: the session (or ExecStopPost's
        # restore, if this run is killed) puts them back, so no experiment outlives the run.
        session.save_codec(diag_codec.experiment_state(host))
        if args.model:
            # A reconfig zeroes the mixer: keep the owner's levels so the session puts them back.
            if not session.save_mixer():
                report.add([check("codec.reconfig", "codec", "skip", args.model,
                                  "the mixer could not be saved, so the codec is not reconfigured "
                                  "(a reconfiguration resets the mixer)",
                                  "Check that alsactl works and /run/being-diag is writable, then run again.")])
            else:
                changed = report.add(guarded("codec.reconfig", "codec", diag_codec.reconfigure, host, args.model))
                if any(isinstance(item["value"], dict) and item["value"].get("written") for item in changed):
                    report.add([mixer_after_reconfig(host, True)])
        if args.coef:
            report.add(guarded("codec.coef", "codec", diag_codec.set_coefficient, host, int(args.coef, 16)))
        after = guarded("codec.present", "codec", diag_codec.codec_checks, host, None)
        report.add([{**item, "id": item["id"] + ".after"} for item in after if item["id"] == "codec.adc_cfg"])
        report.add(guarded("mic.floor", "mic", diag_sound.check_mic_floor, host, report,
                           keep=args.yes_keep_recordings))
    report.extra["restore"] = session.restored
    report.add([codec_restored(session.restored)])


def codec_restored(done):
    """The codec.restored check: whether the experiments were undone when the run ended."""
    lines = [line for line in done or [] if "the codec" in line]
    failed = [line for line in lines if line.startswith("could not")]
    if failed:
        return check("codec.restored", "codec", "warn", lines, "; ".join(lines),
                     "The codec is still changed: the microphone may sound different until the next reboot.")
    return check("codec.restored", "codec", "pass", lines,
                 "; ".join(lines) if lines else "the codec was as before the run: nothing to put back")


AUDIO_INIT = diag_sound.AUDIO_INIT


def mixer_after_reconfig(host, saved):
    """A reconfigured codec comes back with its mixer at the driver's defaults (Master and Capture
    off): put the owner's levels back for the measurements, or the working defaults if that fails."""
    if saved:
        result = host.run(["alsactl", "restore", "-f", str(host.path(str(diag_sound.MIXER_STATE))),
                           diag_sound.CARD], timeout=20)
        if result is not None and result.code == 0:
            return check("codec.mixer_reset", "codec", "pass", "owner",
                         "the mixer levels from before the run are back after the reconfiguration")
    item = reset_mixer(host)
    if saved and item["status"] == "pass":
        item["evidence"] += (" (the levels from before the run could not be restored now; the run tries again "
                             "when it ends)")
    return item


def reset_mixer(host):
    """A reconfigured codec comes back with fresh mixer controls: set the working levels again."""
    if not host.exists(str(AUDIO_INIT)):
        return check("codec.mixer_reset", "codec", "skip", None, "being-audio-init is not installed")
    result = host.run([str(host.path(str(AUDIO_INIT))), "--defaults"], timeout=60)
    ok = result is not None and result.code == 0
    return check("codec.mixer_reset", "codec", "pass" if ok else "warn", ok,
                 "being-audio-init --defaults set the mixer for the new codec setup" if ok else
                 "being-audio-init --defaults failed after the reconfiguration",
                 "" if ok else "The microphone may be muted until the next boot.")


def engines_section(host, report, *, bench=False, record_smoke=False):
    ready, why = diag_speech.moonshine_ready()
    report.add([check("engines.moonshine", "engines", "pass" if ready else "skip", ready,
                      why or "installed and verified")])
    piper, why = diag_speech.piper_engine()
    report.add([check("engines.piper", "engines", "pass" if piper else "skip", bool(piper),
                      why or "the natural voice is installed and verified")])
    flags = (host.read("/proc/cpuinfo", 64 * 1024) or "")
    first = next((line for line in flags.splitlines() if line.startswith("flags")), "").split()
    avx, avx2 = "avx" in first, "avx2" in first
    report.add([check("engines.cpu", "engines", "pass", {"avx": avx, "avx2": avx2},
                      f"AVX {'yes' if avx else 'no'}, AVX2 {'yes' if avx2 else 'no'} "
                      "(sherpa-onnx must not need AVX2)")])
    report.add(guarded("engines.bench", "engines", run_voice_bench, host, report))
    if ready or "not installed" not in (why or ""):
        report.add(guarded("engines.smoke", "engines", run_speech_smoke, host, report, record=record_smoke))
    if bench:
        report.add(guarded("engines.model_bench", "engines", run_model_bench, host, report))


def run_voice_bench(host, report):
    output = report.path("engines.json")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    result = host.run([sys.executable, "-m", "mind.voice_bench", "--busy", "none", "--output", str(output)],
                      timeout=1200, env=env)
    if result is None:
        return check("engines.bench", "engines", "warn", None, "the voice bench did not finish in 20 minutes")
    if result.code < 0:
        return check("engines.bench", "engines", "fail", None, f"the voice bench crashed with signal {-result.code}",
                     "A speech engine crashed this CPU's process: keep it switched off.")
    try:
        data = json.loads(output.read_text())
        report.note_file(output)
    except (OSError, ValueError):
        data = None
    verdict = data.get("verdict") if isinstance(data, dict) else None
    failed = sorted(name for name, ok in (verdict or {}).items() if ok is False)
    status = "pass" if result.code == 0 and not failed else "warn"
    return check("engines.bench", "engines", status, verdict,
                 f"voice bench exit {result.code}; verdict {json.dumps(verdict) if verdict else 'none'}",
                 f"Over the speed limits: {', '.join(failed)}. 'auto' keeps the faster engines." if failed else "")


SMOKE_TIMEOUT_S = 3 * 300 + 60


def run_speech_smoke(host, report, *, record=False):
    """The voice's own Ivy Bridge speech smoke on this machine: Silero VAD, a Moonshine decode and a
    Piper sentence, each in a process of its own (an instruction this CPU lacks kills only that one).

    Measured on the laptop: every stage passes, but nothing ever recorded it, so the automatic choice
    never used Moonshine. With `record` the verdict, pass or fail, is kept in /mind/audio, pinned to
    the installed runtime's bytes and this build, where the voice's next engine choice reads it. On a
    Try boot /mind is in RAM, so that lasts only until the reboot, and the check says so."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent), "PYTHONDONTWRITEBYTECODE": "1"}
    argv = [sys.executable, "-m", "mind.voice_engines", "smoke", "--json"] + (["--record"] if record else [])
    result = host.run(argv, timeout=SMOKE_TIMEOUT_S, env=env)
    if result is None:
        return check("engines.smoke", "engines", "warn", None,
                     f"the speech smoke did not finish in {SMOKE_TIMEOUT_S} s")
    found = None
    for line in reversed(result.out.strip().splitlines()):
        try:
            found = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(found, dict):
        if "invalid choice" in result.err:
            return check("engines.smoke", "engines", "skip", None, "this build's voice has no speech smoke command")
        return check("engines.smoke", "engines", "warn", None,
                     f"the speech smoke gave no report (exit {result.code}): {result.err.strip()[-200:]}")
    report.write_text("speech-smoke.json", json.dumps(found, indent=1, sort_keys=True))
    stages = [stage for stage in found.get("stages", []) if isinstance(stage, dict)]
    text = ", ".join(f"{stage.get('stage')} " + ("SIGILL" if stage.get("sigill") else
                                                  "passed" if stage.get("passed") else "failed")
                     for stage in stages) or found.get("error") or "no stages ran"
    value = {"passed": found.get("passed"), "sigill": found.get("sigill"), "recorded": record,
             "stages": [{key: stage.get(key) for key in ("stage", "passed", "sigill", "error")} for stage in stages]}
    # On a Try boot /mind is a tmpfs: a verdict kept there is gone at the next reboot.
    in_ram = diag_system.mount_type(host, "/mind") == "tmpfs"
    value["lasts"] = None if not record else "this boot" if in_ram else "this build"
    kept = ("" if not record else
            "; kept in /mind/audio for this boot only (/mind is in RAM on a Try boot)" if in_ram else
            "; kept in /mind/audio for the voice's automatic choice")
    lasting = ("To keep it across reboots, the image must ship the verdict as "
               "/usr/share/being/voice/sherpa-avx-smoke.json.")
    if found.get("sigill"):
        return check("engines.smoke", "engines", "fail", value, text + kept,
                     "A speech engine needs an instruction this CPU lacks: keep Moonshine off on this build.")
    if found.get("passed") is True:
        if record:
            hint = lasting if in_ram else ""
        elif in_ram:
            hint = ("`pip-diag engines --record-smoke` (as root) lets the automatic choice use Moonshine until "
                    "the next reboot only (/mind is in RAM on a Try boot). " + lasting)
        else:
            hint = ("Run `pip-diag engines --record-smoke` (as root) to let the automatic choice use Moonshine "
                    "on this build.")
        return check("engines.smoke", "engines", "pass", value, text + kept, hint)
    errors = "; ".join(f"{stage.get('stage')}: {stage.get('error')}" for stage in stages if stage.get("error"))
    return check("engines.smoke", "engines", "warn", value, text + (f" ({errors})" if errors else "") + kept,
                 "The speech smoke did not pass: the automatic choice keeps PocketSphinx.")


def run_model_bench(host, report):
    """`bench --quick` beside the cortex, only when memory allows a second model server."""
    available = None
    for line in (host.read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1]) * 1024
    model = host.path("/models/reflex.gguf")
    size = model.stat().st_size if model.exists() else None
    if available is None or size is None or available < size + 1.5e9:
        return check("engines.model_bench", "engines", "skip", {"available": available, "model": size},
                     "not enough free memory for a second model server beside the cortex")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    result = host.run([sys.executable, "-m", "mind.bench", "--quick", "--no-speech", "--dream-model", "",
                       "--output", str(report.folder)], timeout=3600, env=env)
    ok = result is not None and result.code == 0
    return check("engines.model_bench", "engines", "pass" if ok else "warn", None,
                 "bench --quick wrote bench-*.json beside this report" if ok else "bench --quick did not finish")


def commands_section(host, report, args, *, e2e=False):
    report.add(guarded("commands.routes", "commands", diag_speech.check_commands, name=being_name(host)))
    report.add(guarded("workshop.state", "workshop", diag_system.check_workshop, host))
    if not e2e:
        return
    if needs_root(host, report, "hear", "Saying the phrases to the voice"):
        return
    if host.which("flite") is None:
        report.add([check("hear.connect", "hear", "skip", None, "flite is needed to render the phrases")])
        return
    stimuli = []
    for number, (text, expected) in enumerate(diag_speech.E2E_PHRASES, 1):
        try:
            samples, _ = diag_speech.stimulus_from_phrase(host, text)
        except ValueError as error:
            report.add([check(f"hear.render.{number}", "hear", "warn", None, str(error))])
            continue
        stimuli.append((f"p{number}", samples, text, expected))
    report.add(guarded("hear.connect", "hear", diag_speech.run_hear, host, report,
                       diag_speech.at_levels(stimuli, getattr(args, "level", "both")), name=being_name(host)))


def being_name(host):
    try:
        from .profile import FACTORY_CONFIG, load_profile

        return load_profile(FACTORY_CONFIG, Path("/mind")).name or "Pip"
    except Exception:
        return "Pip"


def hear_section(host, report, args):
    if args.phrase:
        try:
            samples, wav = diag_speech.stimulus_from_phrase(host, args.phrase, args.voice)
        except ValueError as error:
            raise CannotRun(f"could not render the phrase: {error}") from None
        label, expected_text = "phrase", args.phrase
        report.extra["stimulus"] = {"phrase": args.phrase, "voice": args.voice}
        saved = report.path(f"hear-{args.voice}.wav")  # Pip's own voice: kept
        try:
            saved.write_bytes(wav)
            report.note_file(saved)
        except OSError:
            pass
    else:
        try:
            samples = diag_speech.stimulus_from_file(args.file)
        except (OSError, EOFError, ValueError) as error:
            raise CannotRun(f"could not read {args.file}: {error}") from None
        label, expected_text = "file", None
        report.extra["stimulus"] = {"file": str(args.file)}
    name = being_name(host)
    expected_route = args.expect_route or (expected_route_now(host, expected_text, name) if expected_text else None)
    report.add(guarded("hear.connect", "hear", diag_speech.run_hear, host, report,
                       diag_speech.at_levels([(label, samples, expected_text, expected_route)], args.level),
                       asr=args.asr, name=name, patience=args.patience))


def expected_route_now(host, text, name):
    """The route `text` should take with the workshop as it is now.

    "Close the terminal" with the terminal open is workshop.close. "Open a
    terminal" stays workshop.open even when one is already open (where the
    mind rightly answers workshop.say): the hear checks then warn that the
    run proves nothing, instead of passing it."""
    closed = diag_speech.predict_route(text, name)
    if closed == "workshop.open":
        return closed
    return diag_speech.predict_route(text, name, ctx=app_context(host))


def app_context(host):
    """The workshop as it is now, for predicting a route ("close the terminal" means
    something else with the terminal open). A closed terminal when nothing can tell."""
    from .app_commands import AppContext

    try:
        opened = diag_system.terminal_open(host)[0]
    except Exception:
        opened = False
    return AppContext(terminal="shown" if opened else "closed")


def voice_section(host, report, args, reader=input):
    """The owner-present flow. Prompts go to the terminal; Enter moves on."""
    if args.json:
        raise CannotRun("the voice checks are interactive: run them without --json")
    consent = args.yes_keep_recordings or report.ask(
        "Keep recordings and transcripts on the stick?", default=False, reader=reader)
    report.meta["keep_recordings"] = consent
    report.keep_speech = consent
    keeper = diag_speech.Keeper(report, consent)
    sphinx = sphinx_or_skip(report, "voice")
    try:
        with diag_sound.AudioSession(host) as session:
            channel = None
            try:
                check_item, channel = diag_speech.mic_speech(host, report, keeper, session, reader)
                report.add([check_item])
            except Exception as error:  # the owner's time is not wasted: go on to the next step
                report.add([check("voice.mic_speech", "voice", "warn", None, f"the check itself failed: {error!r}")])
            count = max(len(diag_speech.CORE_PHRASES), min(40, args.phrases))
            table = list(diag_speech.COMMAND_PHRASES)
            phrases = [item for item in table if item[0] in diag_speech.CORE_PHRASES]
            phrases += [item for item in table if item not in phrases][:max(0, count - len(phrases))]
            report.add(guarded("voice.corpus", "voice", diag_speech.corpus, host, report, keeper, sphinx, reader,
                               channel, phrases))
            voices, _ = diag_speech.available_voices(host)
            files = []
            for name, engine in voices:
                path = report.path(f"listen-{name}.wav")
                if "error" not in diag_speech.synthesize(host, name, "Sorry, I didn't catch that.", path,
                                                         piper=engine):
                    report.note_file(path)
                    files.append((name, path))
            channel_file = diag_sound.speaker_stimuli(report, args.stimulus_level,
                                                      {"channels-48k"}).get("channels-48k", (None,))[0]
            report.add(guarded("voice.listening", "voice", diag_speech.listening, host, report, reader, files,
                               channel_file))
        report.extra["restore"] = session.restored
        journal = Journal(host)
        cursor = journal.cursor()
        journal.wait(cursor, lambda entry: "listening for the wake phrase" in entry.message, timeout=40,
                     units=(diag_sound.VOICE,))
        report.add(guarded("voice.wake_1m", "voice", diag_speech.wake_trials, host, report, reader, journal,
                           trials=args.trials, distance="1m"))
        if args.two_metres:
            report.add(guarded("voice.wake_2m", "voice", diag_speech.wake_trials, host, report, reader, journal,
                               trials=args.trials, distance="2m"))
        report.add(guarded("voice.false_wakes", "voice", diag_speech.false_wakes, host, report, reader, journal))
    finally:
        keeper.close()
        if sphinx is not None:
            sphinx.close()


# Running -----------------------------------------------------------------------------

def run(host, args, report, reader=input):
    command = args.command
    if command == "quick":
        system_section(host, report, args)
        codec_section(host, report)
        commands_section(host, report, args)
        cortex_section(host, report)
        body_section(host, report)
        if host.is_root() and sound_card(host):
            audio_block(host, report, args, mic=True, quick_speaker=True)
        else:
            report.add(guarded("mic.mixer", "mic", diag_sound.check_mixer, host, report))
        tts_section(host, report, selftest=False)
    elif command == "full":
        system_section(host, report, args)
        codec_section(host, report)
        cortex_section(host, report, full=True)
        body_section(host, report, full=True)
        audio_block(host, report, args, mic=True, sweep=True, speaker=True, loop=True, isolation=True, tts=True,
                    acoustic=True)
        engines_section(host, report, bench=True)
        commands_section(host, report, args, e2e=diag_system.voice_debug_on(host) and host.is_root())
    elif command == "commands":
        commands_section(host, report, args, e2e=args.e2e or (diag_system.voice_debug_on(host) and host.is_root()))
    elif command == "workload":
        report.add(guarded("workload.routes", "workload", diag_speech.check_route_sample, host, being_name(host)))
        report.add(guarded("workload.reflex_pressure", "workload", diag_system.check_reflex_pressure, host))
        report.add(guarded("workload.recall", "workload", diag_system.check_recall_timing, host))
    elif command == "continuity":
        report.add(guarded("continuity.archive", "continuity", diag_system.check_continuity_archive, host))
        report.add(guarded("continuity.slots", "continuity", diag_system.check_continuity_slots, host))
        report.add(guarded("continuity.baseline", "continuity", diag_system.check_continuity_baseline, host))
        report.add(guarded("continuity.recall", "continuity", diag_system.check_continuity_recall))
    elif command == "mic":
        audio_block(host, report, args, mic=True, sweep=True)
    elif command == "speaker":
        audio_block(host, report, args, speaker=True, loop=True, isolation=True)
    elif command == "echo-trace":
        echo_trace_section(host, report, args)
    elif command == "native-echo-trace":
        native_echo_trace_section(host, report, args)
    elif command == "tts":
        audio_block(host, report, args, tts=True, acoustic=True)
    elif command == "codec":
        codec_section(host, report, args)
    elif command == "hear":
        hear_section(host, report, args)
    elif command == "engines":
        engines_section(host, report, record_smoke=args.record_smoke)
    elif command == "voice":
        voice_section(host, report, args, reader)


def validate(host, args):
    if args.command in ROOT_ONLY and not host.is_root():
        raise CannotRun(f"`pip-diag {args.command}` changes the sound card or the voice and needs root")
    if args.command in AUDIO and host.path("/run/being/mic-policy-breach").exists():
        raise CannotRun("The microphone safety latch is active until reboot. Read-only diagnostics are available.")
    if not -40.0 <= args.stimulus_level <= -3.0:
        raise CannotRun("--stimulus-level must be between -40 and -3 dBFS")
    if args.command == "hear":
        if bool(args.file) == bool(args.phrase):
            raise CannotRun("give either a WAV file or --phrase TEXT")
        if args.file:
            args.file = str(Path(args.file).resolve())
        if not 1.0 <= args.patience <= 20.0:
            raise CannotRun("--patience must be between 1 and 20")
    if args.command == "engines" and args.record_smoke and not host.is_root():
        raise CannotRun("`pip-diag engines --record-smoke` writes the voice's state in /mind and needs root")
    if args.command in ("echo-trace", "native-echo-trace"):
        if not args.consent_room_audio:
            raise CannotRun(f"{args.command} requires --consent-room-audio")
        output = Path(args.out).resolve() if args.out else None
        if output is None or output.parent != Path("/run/being-diag"):
            raise CannotRun(f"{args.command} requires a new folder directly under /run/being-diag")
        args.out = str(output)
    if args.command == "codec" and args.coef is not None:
        try:
            value = int(args.coef, 16)
        except ValueError:
            raise CannotRun("--coef takes a hexadecimal value such as 0x102a") from None
        if not 0 <= value <= 0xFFFF:
            raise CannotRun("--coef must fit 16 bits")


def lock(host):
    """Only one diagnostics run at a time touches the card. Returns the held file."""
    import fcntl

    path = host.path(str(LOCK))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise CannotRun("another pip-diag run is using the sound card") from None
    return handle


def say_quietly(text, quiet):
    """Print a line for `restore`: stdout normally, stderr with --quiet. Never raises
    (a dropped SSH pipe must not stop the restore from finishing)."""
    try:
        print(text, file=sys.stderr if quiet else sys.stdout, flush=True)
    except (OSError, ValueError):
        pass


def restore_command(host, args):
    """`pip-diag restore`: put things back, unless another live run still owns them.

    It takes the run lock first. A live run (wrapped or `--no-unit`) holds it
    and puts everything back itself when it ends; restoring under it would
    pull the diagnostic microphone and the mixer from under its measurements.
    ExecStopPost of a wrapped run that could not get the lock lands here too."""
    held = None
    try:
        held = lock(host)
    except CannotRun:
        marker = diag_sound.read_marker(host) or {}
        owner = marker.get("pid")
        if not args.force:
            say_quietly("pip-diag restore: another pip-diag run" + (f" (pid {owner})" if owner else "")
                        + " is using the sound card and will put everything back when it ends; "
                          "nothing done (use --force to restore anyway)", args.quiet)
            return 0
    except OSError as error:
        if not args.force:
            say_quietly(f"pip-diag restore: cannot take the run lock: {error}", args.quiet)
            return 2
    try:
        done = diag_sound.restore(host)
    finally:
        if held is not None:
            held.close()
    if done or not args.quiet:
        say_quietly("pip-diag restore: " + ("; ".join(done) if done else "nothing to put back"), args.quiet)
    return 0


def wrap_command(host, args, argv):
    """The systemd-run command line for an audio run, or None to run in this process.

    Under systemd-run, ExecStopPost runs `pip-diag restore`, so the voice
    comes back even if this process is killed outright."""
    # engines times the speech engines too: it runs at the voice's weight like the rest.
    if (args.command not in AUDIO and not (args.command in ("quick", "commands", "engines") and host.is_root())) \
            or args.no_unit or os.environ.get("PIP_DIAG_UNIT") or not host.is_root():
        return None
    if host.which("systemd-run") is None or not diag_system.systemd_present(host) or not host.exists(str(WRAPPER)):
        return None
    if diag_system.is_active(host, f"{UNIT_NAME}.service"):
        raise CannotRun("another pip-diag run is active (systemctl stop pip-diag-audio to end it)")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    # --same-dir keeps a relative WAV path meaning the same file. CPUWeight is
    # the voice's own, so speech timings measure what the voice would get.
    # Exit 1 means "a check failed": a diagnostic verdict, never a failed unit
    # in the boot's evidence, so SuccessExitStatus=1. systemd-run --wait then
    # returns 0 for it, so the run inside writes its exit code to a file of
    # this process's own and wrapped_exit() returns that (see record_verdict).
    return ["systemd-run", f"--unit={UNIT_NAME}", "--wait", "--collect", "--quiet", "--same-dir",
            "--service-type=exec", "-p", f"ExecStopPost={WRAPPER} restore --quiet",
            "-p", f"CPUWeight={VOICE_CPU_WEIGHT}", "-p", "SuccessExitStatus=1",
            "--setenv=PIP_DIAG_UNIT=1", f"--setenv=PIP_DIAG_VERDICT={verdict_file(host)}",
            "--setenv=PYTHONPATH=/usr/lib/being",
            "--pty" if interactive else "--pipe", str(WRAPPER), *argv]


def verdict_file(host):
    """Where the run inside the unit leaves its exit code for this process."""
    return host.path(str(RUN_DIR / f"verdict-{os.getpid()}.json"))


def run_wrapped(command):
    """Run the systemd-run command with this terminal; its exit code, or None if it could not start."""
    import subprocess

    try:
        return subprocess.run(command, check=False).returncode
    except OSError:
        return None


def wrapped_exit(code, verdict):
    """This process's exit code after systemd-run returned `code`.

    With SuccessExitStatus=1 systemd-run returns 0 for "a check failed" as
    well as for "nothing failed": the inner run's own code, left in `verdict`,
    tells them apart. Any other code (2: the run could not be done, or a
    signal) is the unit's own and is returned as it is."""
    try:
        data = json.loads(Path(verdict).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    _forget(verdict)
    if code != 0:
        return code
    inner = data.get("code") if isinstance(data, dict) else None
    return inner if inner in (0, 1) else 0


def record_verdict(code):
    """Inside the unit: leave this run's exit code for the systemd-run caller. Never raises."""
    target = os.environ.get("PIP_DIAG_VERDICT") if os.environ.get("PIP_DIAG_UNIT") else None
    if not target:
        return
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"code": code}), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def _forget(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def catch_signals():
    """Ctrl-C, SIGTERM and a dropped SSH session (SIGHUP) end the run cleanly, with the report saved."""
    def stop(signum, _frame):
        raise Interrupted(signum)

    previous = {}
    for number in diag_sound.SIGNALS:
        try:
            previous[number] = signal.signal(number, stop)
        except (ValueError, OSError):  # not the main thread (tests)
            pass
    return previous


def release_signals(previous):
    for number, handler in previous.items():
        try:
            signal.signal(number, handler)
        except (ValueError, OSError):
            pass


def mark_active(host, command, folder):
    """Write (or, with command None, remove) /run/being-diag/active.json. Never raises."""
    path = host.path(str(ACTIVE))
    try:
        if command is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"command": command, "folder": str(folder), "pid": os.getpid(),
                                        "started_utc": utc_stamp()}), encoding="utf-8")
    except OSError:
        pass


def show_report(args):
    path = Path(args.folder) / "diag.json" if args.folder else latest_report()
    if path is None or not path.is_file():
        raise CannotRun("no diagnostics report found")
    data = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        for item in data.get("checks", []):
            print(json.dumps(item, separators=(",", ":")))
    else:
        text = (path.parent / "diag.txt")
        print(text.read_text(encoding="utf-8") if text.is_file() else json.dumps(data, indent=1))
    return exit_code(data.get("checks", []))


def main(argv=None, *, host=None, reader=input):
    argv = list(sys.argv[1:] if argv is None else argv)
    code = _main(argv, host=host, reader=reader)
    # The unit's ExecStopPost `pip-diag restore` runs with the same environment:
    # only the measuring run leaves its verdict.
    if argv[:1] not in (["restore"], ["report"]):
        record_verdict(code)
    return code


def _main(argv=None, *, host=None, reader=input):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    host = host or Host()
    try:
        if args.command == "report":
            return show_report(args)
        validate(host, args)
        if args.command == "restore":
            return restore_command(host, args)
        wrapped = wrap_command(host, args, argv)
        if wrapped is not None:
            verdict = verdict_file(host)
            _forget(verdict)
            code = run_wrapped(wrapped)
            if code is not None:
                return wrapped_exit(code, verdict)
        held = lock(host) if args.command in AUDIO or host.is_root() else None
        try:
            if args.command in ("echo-trace", "native-echo-trace"):
                folder = Path(args.out)
                folder.parent.mkdir(mode=0o700, exist_ok=True)
                parent = folder.parent.lstat()
                if parent.st_uid != 0 or parent.st_mode & 0o022 or not folder.parent.is_dir():
                    raise OSError("the echo trace parent must be root-owned and protected")
                if folder.parent.resolve() != folder.parent:
                    raise OSError("the echo trace parent must be a real RAM directory")
                folder.mkdir(mode=0o700)
            else:
                folder = output_folder(args.out)
        except OSError as error:
            if held is not None:
                held.close()
            raise CannotRun(f"cannot write the report folder: {error}") from None
        report = Report(folder, meta(host, args, folder), stream_json=args.json,
                        keep_speech=args.yes_keep_recordings)
        report.save()
        mark_active(host, args.command, folder)
        report.say(f"pip-diag {args.command}: writing to {folder}")
        interrupted = None
        failed = None
        previous = catch_signals()
        try:
            run(host, args, report, reader)
        except KeyboardInterrupt:
            interrupted = signal.SIGINT
            report.meta["interrupted"] = f"signal {signal.SIGINT}"
        except Interrupted as stop:
            interrupted = stop.signum
            report.meta["interrupted"] = f"signal {stop.signum}" if stop.signum else "output closed"
        except CannotRun:
            raise
        except Exception as error:  # a fault in pip-diag itself: keep what was measured, say so
            failed = f"{type(error).__name__}: {error}"
            report.meta["internal_error"] = failed
            try:
                report.add([check("diag.internal", "diag", "warn", None, f"pip-diag stopped early: {failed}",
                                  "This is a fault in pip-diag, not in Pip. The checks above still stand.")])
            except Interrupted:
                pass
        finally:
            release_signals(previous)
            report.finish()
            mark_active(host, None, folder)
            if held is not None:
                held.close()
        if interrupted is not None or report.meta.get("interrupted"):
            report.say("pip-diag: interrupted; the voice and mixer were put back")
            return 2
        if failed is not None:
            print(f"pip-diag: stopped early: {failed}", file=sys.stderr)
            return 2
        report.say(f"pip-diag: wrote {folder / 'diag.txt'}")
        report.say("")
        for line in (folder / "diag.txt").read_text(encoding="utf-8").split("Every check")[0].splitlines():
            report.say(line)
        return exit_code(report.checks)
    except CannotRun as error:
        print(f"pip-diag: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

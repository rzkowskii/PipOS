"""The sound card under test: the voice set aside, recordings, playback, mixer.

`hw:0,0` is exclusive and the image has no dmix, so every section that opens
the card first stops Voice and the isolated Ears. `AudioSession` does that safely:

1. it writes the marker /run/being-diag/voice-stopped (what to put back);
2. it stops Voice, then Ears (so Voice releases its stream first), and for `hear` installs the diagnostic-microphone
   drop-in;
3. on the way out (normally, on an exception, or on SIGINT, SIGTERM or
   SIGHUP from a dropped SSH session) `restore()` removes the drop-in, puts
   the mixer back, restores Ears before Voice, and removes the marker.

`pip-diag restore` runs the same `restore()` by hand, and the systemd-run
wrapper in mind/diag.py runs it as ExecStopPost, so even a killed run puts
the voice back.

Sections: mic floor (per channel, dead-channel detection), mixer (with the
kept-state override), the Mic Boost x Capture sweep, the speaker battery
(rate, underruns, the `aplay -v` chain, 16000, 22050 and 48000 Hz files),
the acoustic loopback (level and THD judged against a silence recording
through the same path, latency from the tone's own onset, octave bands,
dropouts, the 22050 Hz path's clock drift) and the speaker isolation per
band and per channel.
"""

import json
import os
import re
import signal
import tempfile
import time
from pathlib import Path

from . import diag_audio as audio
from . import diag_codec
from .diag_report import RUN_DIR, Interrupted, check
from .diag_system import systemctl, unit_state

VOICE = "being-voice.service"
EARS = "being-ears.service"
EARS_UNIT = Path("/usr/lib/systemd/system/being-ears.service")
MARKER = RUN_DIR / "voice-stopped"
AUDIO_INIT = Path("/usr/libexec/being/being-audio-init")
NATIVE_DUPLEX = Path("/usr/libexec/being/being-audio-duplex")
MIXER_STATE = RUN_DIR / "mixer.state"
MIC_SOCKET = RUN_DIR / "mic.sock"
# Room recordings nobody agreed to keep are made here (RAM), never on the stick.
SCRATCH = RUN_DIR / "tmp"
# A unit in one of these states is meant to be running: `activating` covers a
# Restart=always unit waiting out RestartSec, or a queued --no-block restart.
WANTED_STATES = ("active", "activating", "reloading", "failed")
DROPIN_DIR = Path("/run/systemd/system/being-voice.service.d")
DROPIN = DROPIN_DIR / "50-diag-mic.conf"
KEPT_STATE = Path("/mind/audio/asound.state")
CARD = "0"
CAPTURE_DEVICE = "hw:0,0"
PLAY_DEVICE = "plughw:0,0"
PCM_PLAYBACK = "/proc/asound/card0/pcm0p/sub0"
PCM_CAPTURE = "/proc/asound/card0/pcm0c/sub0"
DEAD_DBFS = -80.0
DIGITAL_SILENCE_DBFS = -90.0
ONE_SIDE_DB = 12.0
RATE_TOLERANCE = 0.02
SIGNALS = tuple(getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name))
BOOSTS = (0, 1, 2, 3)
# A loopback recording starts this long before the stimulus plays.
PREROLL_S = 0.3
CAPTURES = (51, 57, 63)
PIPER_RATE = 22050
# The microphone hears the speaker when the tone stands this far above the room floor
# (in level, or in the tone's own frequency bin).
HEARD_DB = 15.0
# THD is read in this window after the tone's onset (it starts with a 10 ms fade).
THD_START_S = 0.3
THD_SECONDS = 2.0
THD_WARN_PERCENT = 10.0
# Clocks further apart than this are more than a resampling quirk.
DRIFT_WARN_PPM = 1000.0
# Speaker isolation: the pins split the band (on the MacBookPro9,2 Bass Speaker 0x0a
# carries 300-1200 Hz, Speaker 0x0b 1.2-2.4 kHz), so isolation is measured per band.
ISOLATION_BANDS = ((300, 1200), (1200, 2400), (2400, 8000))
WIDE_BAND = (300, 8000)
ISOLATION_SECONDS = 2.0
# A pin or channel counts as playing when it stands this far above the floor in a band.
PLAYS_DB = 6.0


# Setting the voice aside ------------------------------------------------------

def read_marker(host):
    text = host.read(str(MARKER))
    if text is None:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}  # unreadable: assume the voice was running, and put it back


def write_marker(host, state):
    host.path(str(RUN_DIR)).mkdir(parents=True, exist_ok=True)
    temporary = host.path(str(MARKER) + ".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    os.replace(temporary, host.path(str(MARKER)))


def dropin_text(env):
    lines = ["# Written by pip-diag for one diagnostics run; removed when it ends.", "[Service]"]
    lines += [f"Environment={key}={value}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def voice_wanted(host):
    """Should the voice be running? True unless it is plainly inactive (or nothing can tell).

    `systemctl is-active` says `activating` while being-voice (Restart=always)
    waits to come back after a crash, and `failed` when it gave up: both mean
    the owner wants a voice, so the run puts it back. Only `inactive` (stopped
    on purpose, or never started on this boot) leaves it stopped."""
    return unit_state(host, VOICE) in WANTED_STATES


def ears_wanted(host):
    return unit_state(host, EARS) in WANTED_STATES


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, OverflowError, OSError):
        return True  # exists (or cannot tell): leave its files alone
    return True


def _clear_scratch(host):
    """Remove room recordings a killed run left in RAM (files named after a pid no longer alive)."""
    folder = host.path(str(SCRATCH))
    removed = 0
    try:
        names = list(folder.iterdir()) if folder.is_dir() else []
    except OSError:
        return []
    for path in names:
        prefix = path.name.split("-", 1)[0]
        if prefix.isdigit() and int(prefix) != os.getpid() and not pid_alive(int(prefix)):
            _remove(path)
            removed += 1
    return [f"removed {removed} recordings an interrupted run left in RAM"] if removed else []


def restore(host):
    """Put the voice and the mixer back after a diagnostics run. Idempotent; never raises.

    The caller must hold the run lock (or know no run is live): it removes the
    diagnostic microphone and the marker of whichever run wrote them.

    A voice that ran with the diagnostic drop-in is always restarted or
    stopped, so no process keeps BEING_DIAG_MIC (it would hear digital
    silence from a closed socket until the next restart).

    Returns the list of things it did, for the log and `pip-diag restore`."""
    done = []
    marker = read_marker(host)
    dropin = host.path(str(DROPIN))
    had_dropin = dropin.exists()
    try:
        if had_dropin:
            dropin.unlink()
            done.append("removed the diagnostic microphone drop-in")
            systemctl(host, "daemon-reload")
    except OSError as error:
        done.append(f"could not remove the drop-in: {error}")
    state = host.path(str(MIXER_STATE))
    reconfigured = False
    if marker and marker.get("codec"):
        # Before the mixer: putting the codec model back reconfigures it, which resets the mixer.
        codec_done, reconfigured = diag_codec.put_back(host, marker["codec"])
        done += codec_done
    if marker and marker.get("mixer_saved") and state.exists():
        result = host.run(["alsactl", "restore", "-f", str(state), CARD], timeout=20)
        done.append("restored the mixer" if result is not None and result.code == 0 else "could not restore the mixer")
    elif reconfigured and host.exists(str(AUDIO_INIT)):
        result = host.run([str(host.path(str(AUDIO_INIT))), "--defaults"], timeout=60)
        ok = result is not None and result.code == 0
        done.append("set the working mixer levels after the codec reconfiguration" if ok else
                    "could not set the mixer after the codec reconfiguration")
    if marker and marker.get("dump_coef") is not None:
        host.write("/sys/module/snd_hda_codec/parameters/dump_coef", str(marker["dump_coef"]))
    if marker is not None or had_dropin:
        wanted = True if marker is None else marker.get("voice_was_active", True)
        used_dropin = had_dropin or bool(marker and marker.get("dropin"))
        # New markers name Ears explicitly. Old markers/images never stopped
        # it, so absence preserves the legacy Voice-only restore.
        if marker is not None and "ears_was_active" in marker:
            if marker.get("ears_was_active"):
                ok = systemctl(host, "--no-block", "restart", EARS)
                done.append("restarted the ears" if ok else "could not restart the ears")
            else:
                ok = systemctl(host, "stop", EARS)
                done.append("left the ears stopped" if ok else "could not stop the ears")
        if wanted:
            # A restart, not a start: a voice running with the drop-in's
            # environment must come back without it. Restore the mixer first
            # so the new voice sees and respects an existing playback mute.
            ok = systemctl(host, "--no-block", "restart", VOICE)
            done.append("restarted the voice" if ok else "could not restart the voice")
        elif used_dropin:
            # The run started the voice on the diagnostic microphone itself;
            # it was not running before, so it goes back to not running.
            ok = systemctl(host, "stop", VOICE)
            done.append("stopped the voice the run had started" if ok else "could not stop the voice")
    done += _clear_scratch(host)
    for path in (MARKER, MIXER_STATE, MIC_SOCKET):
        try:
            host.path(str(path)).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            done.append(f"could not remove {path}: {error}")
    return done


class AudioSession:
    """Voice/Ears stopped (or Voice on the diagnostic microphone) for a while.

    Use as a context manager. `restore()` runs on the way out however the
    block ends, including SIGINT, SIGTERM and SIGHUP, which raise
    Interrupted inside the block.
    """

    def __init__(self, host, *, dropin_env=None, signals=True):
        self.host = host
        self.dropin_env = dropin_env
        self.use_signals = signals
        self.previous = {}
        self.children = []
        self.state = {}
        self.restored = None
        self.inherited = False
        self.deferred = None  # a signal that arrived while restoring

    def _handler(self, signum, _frame):
        raise Interrupted(signum)

    def _defer(self, signum, _frame):
        """During restore: remember the stop request and honour it once everything is back."""
        if self.deferred is None:
            self.deferred = signum

    def _install(self):
        if not self.use_signals:
            return
        for number in SIGNALS:
            try:
                self.previous[number] = signal.signal(number, self._handler)
            except (ValueError, OSError):  # not the main thread
                pass

    def _uninstall(self, defer=False):
        for number, previous in list(self.previous.items()):
            try:
                signal.signal(number, self._defer if defer else previous)
            except (ValueError, OSError):
                pass
        if not defer:
            self.previous.clear()

    def _initial_state(self):
        """What to put back. A marker left by a run that died before restoring
        still knows the voice and mixer as they were before it; that is kept."""
        state = {"voice_was_active": voice_wanted(self.host), "dropin": bool(self.dropin_env),
                 "mixer_saved": False, "pid": os.getpid(), "started": round(time.time())}
        if self.host.exists(str(EARS_UNIT)):
            state["ears_was_active"] = ears_wanted(self.host)
        stale = read_marker(self.host)
        if stale is not None:
            self.inherited = True
            state["voice_was_active"] = state["voice_was_active"] or stale.get("voice_was_active", True)
            if "ears_was_active" in state and "ears_was_active" in stale:
                state["ears_was_active"] = state["ears_was_active"] or stale.get("ears_was_active", True)
            state["dropin"] = state["dropin"] or bool(stale.get("dropin"))
            if stale.get("mixer_saved") and self.host.path(str(MIXER_STATE)).exists():
                state["mixer_saved"] = True  # the earlier run's copy is the mixer as it really was
            if stale.get("dump_coef") is not None:
                state["dump_coef"] = stale["dump_coef"]
            if stale.get("codec"):
                state["codec"] = stale["codec"]  # the codec as it was before the interrupted run's experiments
            state["inherited_from"] = stale.get("pid")
        return state

    def __enter__(self):
        self._install()
        try:
            self.state = self._initial_state()
            write_marker(self.host, self.state)
            # Voice closes its authenticated Ears stream first; only then may
            # Ears terminate arecord and release the kernel PCM.
            systemctl(self.host, "stop", VOICE)
            if "ears_was_active" in self.state:
                systemctl(self.host, "stop", EARS)
            if self.dropin_env:
                self.host.path(str(DROPIN_DIR)).mkdir(parents=True, exist_ok=True)
                self.host.path(str(DROPIN)).write_text(dropin_text(self.dropin_env), encoding="utf-8")
                systemctl(self.host, "daemon-reload")
        except BaseException as error:
            self.__exit__(type(error), error, None)
            raise
        return self

    def start_voice(self):
        """Start the voice with the drop-in's environment (for `hear`)."""
        return systemctl(self.host, "restart", VOICE)

    def save_mixer(self):
        if self.inherited and self.state.get("mixer_saved") and self.host.path(str(MIXER_STATE)).exists():
            return True  # never overwrite the pre-run mixer an interrupted run saved
        result = self.host.run(["alsactl", "store", "-f", str(self.host.path(str(MIXER_STATE))), CARD], timeout=20)
        if result is not None and result.code == 0:
            self.state["mixer_saved"] = True
            write_marker(self.host, self.state)
            return True
        return False

    def save_codec(self, codec_state):
        """Keep the codec as it is before a live experiment, so restore() (or ExecStopPost's,
        if this process is killed) puts it back. An interrupted run's copy is never replaced."""
        if not self.state.get("codec"):
            self.state["codec"] = codec_state
            write_marker(self.host, self.state)

    def track(self, child):
        if child is not None:
            self.children.append(child)
        return child

    def __exit__(self, exc_type, *_):
        self._uninstall(defer=True)  # nothing may cut the restore short; a stop request waits for it
        try:
            for child in self.children:
                try:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                except Exception:
                    pass
            self.restored = restore(self.host)
        finally:
            self._uninstall()
        if self.deferred is not None and exc_type is None:
            raise Interrupted(self.deferred)  # the voice is back: now stop as asked
        return False


# ALSA -------------------------------------------------------------------------

def parse_fields(text):
    """`key: value` lines of /proc/asound .../hw_params and status."""
    fields = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def hw_rate(fields):
    match = re.match(r"(\d+)", fields.get("rate", ""))
    return int(match[1]) if match else None


def pointer_ratio(samples, rate):
    """Playback speed from the hardware pointer: 1.0 is exact. None without two usable samples."""
    usable = []
    for fields, clock in samples:
        try:
            pointer = int(fields["hw_ptr"])
        except (KeyError, ValueError):
            continue
        stamp = fields.get("tstamp")
        try:
            clock = float(stamp) if stamp else clock
        except ValueError:
            pass
        if fields.get("state") == "RUNNING":
            usable.append((pointer, clock))
    if len(usable) < 2 or not rate:
        return None
    (first_pointer, first_clock), (last_pointer, last_clock) = usable[0], usable[-1]
    elapsed = last_clock - first_clock
    if elapsed < 0.3 or last_pointer <= first_pointer:
        return None
    return round((last_pointer - first_pointer) / rate / elapsed, 4)


def aplay_argv(path, device=PLAY_DEVICE, *, period_us=None, buffer_us=None):
    argv = ["aplay", "-v", "-D", device]
    if period_us:
        argv.append(f"--period-time={int(period_us)}")
    if buffer_us:
        argv.append(f"--buffer-time={int(buffer_us)}")
    return argv + [str(path)]


def play(host, path, device=PLAY_DEVICE, *, timeout=60, poll=0.25, status_dir=PCM_PLAYBACK, period_us=None,
         buffer_us=None, authorized=None):
    """Play a WAV with `aplay -v`, timing it and sampling the PCM status while it runs.

    `period_us` and `buffer_us` ask aplay for that period and buffer time
    (the card picks the nearest it supports; hw_params says what it got)."""
    seconds = audio.wav_seconds(path)
    started = host.monotonic()
    child = host.spawn(aplay_argv(path, device, period_us=period_us, buffer_us=buffer_us))
    if child is None:
        return {"device": device, "error": "aplay is not available", "wav_s": round(seconds, 3)}
    samples, hw_params = [], {}
    try:
        while child.poll() is None:
            if host.monotonic() - started > timeout:
                child.kill()
                break
            if authorized is not None and not authorized():
                child.kill()
                child.wait(timeout=5)
                return {"device": device, "file": Path(path).name, "code": 1,
                        "wav_s": round(seconds, 3), "wall_s": round(host.monotonic() - started, 3),
                        "underruns": 0, "hw_params": hw_params, "pointer_ratio": None, "chain": [],
                        "error": "lid or brainstem authorization ended"}
            host.sleep(poll)
            fields = parse_fields(host.read(f"{status_dir}/status", 8192))
            if fields:
                samples.append((fields, host.monotonic()))
            if not hw_params:
                hw_params = parse_fields(host.read(f"{status_dir}/hw_params", 8192))
        _, err = child.communicate(timeout=10)
    except BaseException:
        child.kill()
        raise
    wall = host.monotonic() - started
    err = err.decode("utf-8", "replace") if isinstance(err, bytes) else (err or "")
    rate = hw_rate(hw_params)
    return {"device": device, "file": Path(path).name, "code": child.returncode, "wav_s": round(seconds, 3),
            "wall_s": round(wall, 3), "underruns": err.count("underrun"), "hw_params": hw_params,
            "pointer_ratio": pointer_ratio(samples, rate), "chain": _chain(err),
            "error": None if child.returncode == 0 else err.strip()[-300:]}


def _chain(text):
    """The plugin chain lines of `aplay -v` (shows a linear rate converter when there is one)."""
    keep = [line.strip() for line in (text or "").splitlines()
            if re.search(r"(?i)plug|rate conversion|converter|route|hw|slave|Its setup", line)]
    return keep[:24]


def record_argv(path, device, rate, channels, seconds):
    return ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(rate), "-c", str(channels),
            "-d", str(max(1, int(round(seconds)))), "--dump-hw-params", str(path)]


def record(host, path, *, device=CAPTURE_DEVICE, rate=16000, channels=2, seconds=5, timeout=None):
    """Record a WAV. {code, overruns, hw_params, error}."""
    result = host.run(record_argv(path, device, rate, channels, seconds), timeout=timeout or seconds + 15)
    if result is None:
        return {"code": None, "error": "arecord is not available or timed out", "device": device, "rate": rate}
    return {"code": result.code, "device": device, "rate": rate, "channels": channels,
            "overruns": result.err.count("overrun"), "hw_params": result.err[-1500:],
            "error": None if result.code == 0 else result.err.strip()[-300:]}


CAPTURE_TRIES = ((CAPTURE_DEVICE, 16000), (CAPTURE_DEVICE, 48000), (CAPTURE_DEVICE, 44100), ("plughw:0,0", 16000))


def record_any(host, path, *, seconds=5, channels=2, tries=CAPTURE_TRIES):
    """The first capture setting the card accepts (hw:0,0 is exact or nothing)."""
    attempts = []
    for device, rate in tries:
        result = record(host, path, device=device, rate=rate, channels=channels, seconds=seconds)
        attempts.append(f"{device}@{rate}: {'ok' if result['code'] == 0 else result.get('error')}")
        if result["code"] == 0 and Path(path).exists():
            result["attempts"] = attempts
            return result
        if result["code"] is None:
            break
    return {"code": 1, "error": "; ".join(attempts) or "no capture tried", "attempts": attempts}


def analyse_recording(path, settle=0.1):
    rate, channels = audio.read_wav(path)
    start = int(settle * rate)
    channels = [channel[start:] for channel in channels]
    return rate, channels, audio.channel_levels(channels)


# Mic floor and mixer ------------------------------------------------------------

def judge_channels(levels):
    """(status, evidence, hint) for the per-channel room floor."""
    text = ", ".join(f"{name} rms {level['rms_dbfs']} peak {level['peak_dbfs']} dBFS"
                     for name, level in zip(("left", "right", "ch3", "ch4"), levels))
    if all(level["rms_dbfs"] <= DIGITAL_SILENCE_DBFS for level in levels):
        return "fail", text, ("Digital silence on every channel: the mixer came up muted. "
                              "Run `being-audio-init --defaults` or `pip-diag restore`.")
    if len(levels) >= 2:
        left, right = levels[0]["rms_dbfs"], levels[1]["rms_dbfs"]
        if min(left, right) <= DEAD_DBFS and abs(left - right) >= ONE_SIDE_DB:
            side = "left" if left < right else "right"
            return "warn", f"{text}; the {side} channel is dead", (
                f"Only one side hears. Use being.mic_channel=auto (or {'right' if side == 'left' else 'left'}). "
                "On the MacBookPro9,2 there is one microphone, on the right: the mbp81 fixup only copies it to "
                "the left at 5.8 dB less level.")
    return "pass", text, ""


def scratch_path(host, name):
    """A file for a room recording in RAM (/run/being-diag/tmp): it reaches the
    stick only through keep_recording(), when the owner said yes."""
    folder = host.path(str(SCRATCH))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{os.getpid()}-{name}"
    _remove(path)
    return path


def keep_recording(report, path, name):
    """Move a consented room recording from RAM into the report folder. Never raises."""
    if not Path(path).exists():
        return None
    try:
        target = report.path(name)
        import shutil

        shutil.move(str(path), str(target))
        report.note_file(target)
        return target
    except (OSError, ValueError):
        _remove(path)
        return None


def check_mic_floor(host, report, *, seconds=5, keep=False):
    path = scratch_path(host, "mic-floor.wav")
    try:
        result = record_any(host, path, seconds=seconds)
        if result["code"] != 0:
            return check("mic.floor", "mic", "fail", None, f"recording failed: {result.get('error')}",
                         "The microphone could not be opened. Is something else using the card?")
        try:
            rate, _channels, levels = analyse_recording(path)
        except (OSError, EOFError, ValueError) as error:
            return check("mic.floor", "mic", "fail", None, f"the recording could not be read: {error}")
        if keep:
            keep_recording(report, path, "mic-floor.wav")
    finally:
        _remove(path)  # not kept: the room's sound never reaches the stick
    if report is not None:
        report.write_text("hwparams-capture.txt", result.get("hw_params", ""))
    status, evidence, hint = judge_channels(levels)
    value = {"device": result["device"], "rate": rate, "channels": levels, "overruns": result["overruns"],
             "seconds": seconds}
    if result["overruns"] and status == "pass":
        status, hint = "warn", "The capture overran: the CPU was too busy to read the microphone in time."
    return check("mic.floor", "mic", status, value, f"{result['device']} at {rate} Hz: {evidence}", hint)


def _remove(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


_CONTROL = re.compile(r"^Simple mixer control '([^']+)',(\d+)$")
_VALUE = re.compile(r"^\s+(Front Left|Front Right|Mono): (?:Capture |Playback )?(\d+) \[(\d+)%\](?: \[([-\d.]+)dB\])?")


def parse_scontents(text):
    """{control: {channel: (raw, percent, db)}} from `amixer scontents`."""
    controls, current = {}, None
    for line in (text or "").splitlines():
        match = _CONTROL.match(line)
        if match:
            current = controls.setdefault(match[1], {})
            continue
        match = _VALUE.match(line)
        if match and current is not None:
            current[match[1]] = (int(match[2]), int(match[3]), float(match[4]) if match[4] else None)
    return controls


def check_mixer(host, report=None):
    if host.which("amixer") is None:
        return check("mic.mixer", "mic", "skip", None, "amixer is not installed")
    simple = host.run(["amixer", "-c", CARD, "scontents"], timeout=10)
    contents = host.run(["amixer", "-c", CARD, "contents"], timeout=10)
    if simple is None or simple.code:
        return check("mic.mixer", "mic", "skip", None, "no mixer on card 0")
    if report is not None:
        report.write_text("amixer.txt", simple.out + "\n\n" + (contents.out if contents else ""))
    controls = parse_scontents(simple.out)
    wanted = {name: controls[name] for name in ("Mic Boost", "Capture", "Master", "Speaker", "Bass Speaker",
                                                "Headphone", "PCM", "Digital") if name in controls}
    kept = host.exists(str(KEPT_STATE))
    value = {"controls": wanted, "kept_state_overrides_defaults": kept}
    parts = []
    for name in ("Mic Boost", "Capture"):
        if name in wanted:
            first = next(iter(wanted[name].values()))
            parts.append(f"{name} {first[0]}" + (f" ({first[2]} dB)" if first[2] is not None else ""))
    evidence = ", ".join(parts) or "no capture controls"
    if kept:
        evidence += "; a kept /mind/audio/asound.state replaces the defaults at boot"
    return check("mic.mixer", "mic", "pass", value, evidence,
                 "Saved levels override the defaults; `being-audio-init --defaults` resets them." if kept else "")


def set_control(host, name, value):
    result = host.run(["amixer", "-c", CARD, "-q", "sset", name, str(value)], timeout=10)
    return result is not None and result.code == 0


def current_level(host, name):
    result = host.run(["amixer", "-c", CARD, "sget", name], timeout=10)
    controls = parse_scontents("Simple mixer control '%s',0\n%s" % (name, result.out if result else ""))
    values = controls.get(name) or {}
    return next(iter(values.values()))[0] if values else None


def tone_snr(host, report, tone_path, level_dbfs, name):
    """Record room floor then a played 1 kHz tone; per-channel tone level and SNR at 1 kHz."""
    # The floor is the room alone: recorded in RAM and never kept.
    floor_path, tone_rec = scratch_path(host, f"{name}-floor.wav"), report.path(f"{name}-tone.wav")
    try:
        floor = record_any(host, floor_path, seconds=2, tries=((CAPTURE_DEVICE, 48000), ("plughw:0,0", 48000)))
        if floor["code"] != 0:
            return None
        recorder = host.spawn(record_argv(tone_rec, floor["device"], 48000, 2, 2))
        host.sleep(0.3)
        play(host, tone_path)
        if recorder is not None:
            recorder.wait(timeout=15)
        try:
            _, floor_channels, floor_levels = analyse_recording(floor_path)
            rate, tone_channels, _ = analyse_recording(tone_rec)
        except (OSError, EOFError, ValueError):
            return None
    finally:
        _remove(floor_path)
    report.note_file(tone_rec)  # a known stimulus: kept
    result = []
    for floor_level, channel in zip(floor_levels, tone_channels):
        middle = channel[int(0.4 * rate):int(1.4 * rate)]
        amplitude = audio.goertzel(middle, rate, 1000)
        tone_db = audio.dbfs(amplitude / 2 ** 0.5)
        result.append({"floor_dbfs": floor_level["rms_dbfs"], "tone_dbfs": tone_db,
                       "snr_db": round(tone_db - floor_level["rms_dbfs"], 2)})
    return result


def check_gain_sweep(host, report, session, *, level_dbfs=-20.0, boosts=BOOSTS, captures=CAPTURES):
    """Mic Boost x Capture: room floor and a played tone's SNR for each, mixer restored after."""
    if host.which("amixer") is None or host.which("arecord") is None:
        return check("mic.gain_sweep", "mic", "skip", None, "amixer or arecord is missing")
    if not session.save_mixer():
        return check("mic.gain_sweep", "mic", "skip", None, "the mixer could not be saved, so it is not changed")
    tone_path = report.path("stim-tone1k-sweep.wav")
    left = audio.tone(1000, 1.2, 48000, level_dbfs)
    audio.write_wav(tone_path, [left, left], 48000)
    rows = []
    try:
        for boost in boosts:
            for capture in captures:
                if not (set_control(host, "Mic Boost", boost) and set_control(host, "Capture", capture)):
                    rows.append({"boost": boost, "capture": capture, "error": "the mixer refused"})
                    continue
                measured = tone_snr(host, report, tone_path, level_dbfs, f"sweep-b{boost}-c{capture}")
                rows.append({"boost": boost, "capture": capture, "channels": measured})
    finally:
        result = host.run(["alsactl", "restore", "-f", str(host.path(str(MIXER_STATE))), CARD], timeout=20)
        restored = result is not None and result.code == 0
    good = [row for row in rows if row.get("channels")]
    if not good:
        return check("mic.gain_sweep", "mic", "warn", rows, "no setting gave a usable recording",
                     "Check the speaker and microphone first.")
    def best_snr(row):
        return max(channel["snr_db"] for channel in row["channels"])
    best = max(good, key=best_snr)
    spread = round(best_snr(best) - min(best_snr(row) for row in good), 2)
    evidence = (f"best SNR {best_snr(best)} dB at Mic Boost {best['boost']}, Capture {best['capture']}; "
                f"SNR varies {spread} dB across {len(good)} settings; "
                f"mixer {'restored' if restored else 'NOT restored'}")
    hint = ("The noise sits before the gain stages: more gain will not help recognition."
            if spread < 1.0 else "Gain changes the SNR: consider the best setting above as the default.")
    return check("mic.gain_sweep", "mic", "pass" if restored else "warn", rows, evidence, hint)


# The speaker battery ----------------------------------------------------------------

def speaker_stimuli(report, level_dbfs=-20.0, names=None):
    """Generate the test files (kept: they are Pip's own known signals). {name: (path, kind)}.

    `names` limits it to those files (a quick run needs one tone, not the whole battery)."""
    files = {}

    def add(name, channels, rate, kind):
        if names is not None and name not in names:
            return
        path = report.path(f"stim-{name}.wav")
        audio.write_wav(path, channels, rate)
        report.note_file(path)
        files[name] = (path, kind)

    def wanted(*candidates):
        return names is None or any(name in names for name in candidates)

    for level in (-20, -12, -6, -3):
        if wanted(f"tone1k{level}-16k"):
            mono = audio.tone(1000, 3.0, 16000, level)
            add(f"tone1k{level}-16k", [mono, mono], 16000, "tone")
    if wanted("tone1k-12-48k", "tone1k-12-48k-4ch"):
        tone48 = audio.tone(1000, 3.0, 48000, -12)
        add("tone1k-12-48k", [tone48, tone48], 48000, "tone")
        add("tone1k-12-48k-4ch", [tone48, tone48, tone48, tone48], 48000, "tone")
    if wanted("sweep-48k"):
        sweep = audio.log_sweep(50, 7900, 6.0, 48000, level_dbfs)
        add("sweep-48k", [sweep, sweep], 48000, "sweep")
    if wanted("pink-48k"):
        pink = audio.pink_noise(3.0, 48000, level_dbfs)
        add("pink-48k", [pink, pink], 48000, "noise")
    if wanted("clicks-48k"):
        clicks = audio.click_train(2.0, 48000)
        add("clicks-48k", [clicks, clicks], 48000, "clicks")
    if wanted("silence-48k"):
        quiet = audio.silence(2.0, 48000)
        add("silence-48k", [quiet, quiet], 48000, "silence")
    if wanted("channels-48k"):
        left, right = audio.channel_identity(48000)
        add("channels-48k", [left, right], 48000, "channels")
    # 22050 Hz is the natural voice's own rate; the card has no such rate, so
    # plughw converts it to 44100 Hz, a clock family apart from the 48 kHz capture.
    if wanted("tone1k-12-22k"):
        tone22 = audio.tone(1000, 3.0, PIPER_RATE, -12)
        add("tone1k-12-22k", [tone22, tone22], PIPER_RATE, "tone")
    for rate in (16000, PIPER_RATE, 48000):
        name = f"speech-{rate // 1000}k"
        if wanted(name):
            voiced = audio.speech_like(3.0, rate, -12)
            add(name, [voiced, voiced], rate, "speech")
    # Band-limited noise for the speaker isolation: the two speaker pins carry different bands.
    # Noise peaks well above its RMS: never louder than -12 dBFS RMS, whatever --stimulus-level says.
    band_dbfs = min(level_dbfs, -12.0)
    for low, high in ISOLATION_BANDS:
        name = f"band{low}-{high}-48k"
        if wanted(name):
            band = audio.band_noise(ISOLATION_SECONDS, 48000, low, high, band_dbfs, seed=low)
            add(name, [band, band], 48000, "band")
    for side in ("left", "right"):
        name = f"band{WIDE_BAND[0]}-{WIDE_BAND[1]}-48k-{side}"
        if wanted(name):
            band = audio.band_noise(ISOLATION_SECONDS, 48000, *WIDE_BAND, band_dbfs, seed=5)
            quiet = audio.silence(len(band) / 48000, 48000)
            add(name, [band, quiet] if side == "left" else [quiet, band], 48000, "band")
    return files


def judge_play(result):
    """(status, evidence, hint) of one playback."""
    if result.get("error") or result.get("code"):
        return "fail", f"{result.get('device')}: {result.get('error') or 'aplay failed'}", "Playback failed."
    ratio = result.get("pointer_ratio")
    wall_ratio = round(result["wav_s"] / result["wall_s"], 3) if result.get("wall_s") else None
    text = (f"{result['file']} on {result['device']}: {result['wav_s']} s in {result['wall_s']} s, "
            f"pointer rate {ratio if ratio is not None else '?'}, underruns {result['underruns']}")
    if ratio is not None and abs(ratio - 1.0) > RATE_TOLERANCE:
        return "fail", text, "The card plays at the wrong speed: the voice would sound too high or too low."
    if result["underruns"]:
        return "warn", text, "Playback ran dry: audio stuttered."
    if ratio is None and wall_ratio is not None and wall_ratio < 0.85:
        return "warn", text, "Playback took much longer than the file; no hardware pointer was readable."
    return "pass", text, ""


def check_speaker_battery(host, report, *, level_dbfs=-20.0, full=True, files=None):
    if host.which("aplay") is None:
        return [check("speaker.battery", "speaker", "skip", None, "aplay is not installed")]
    files = files or speaker_stimuli(report, level_dbfs, None if full else {"tone1k-20-16k"})
    plan = [(name, PLAY_DEVICE) for name in files if not name.endswith("4ch")] if full else \
        [("tone1k-20-16k", PLAY_DEVICE)]
    if full:
        plan += [("tone1k-12-48k", CAPTURE_DEVICE), ("tone1k-12-48k-4ch", CAPTURE_DEVICE)]
    checks, chains = [], []
    for name, device in plan:
        result = play(host, files[name][0], device)
        status, evidence, hint = judge_play(result)
        if device == CAPTURE_DEVICE and status == "fail":
            status, hint = "warn", "Not a native format for hw:0,0 (only plughw converts); not a fault by itself."
        chains.append(f"== {name} on {device}\n" + "\n".join(result.get("chain") or []))
        checks.append(check(f"speaker.play.{name}.{'plug' if device == PLAY_DEVICE else 'hw'}", "speaker", status,
                            {key: result.get(key) for key in ("wav_s", "wall_s", "pointer_ratio", "underruns", "code")}
                            | {"rate": hw_rate(result.get("hw_params") or {})}, evidence, hint))
    report.write_text("aplay-chains.txt", "\n\n".join(chains))
    return checks


# Period and buffer sizes ----------------------------------------------------------------

# Small, the driver's default and large periods (aplay --period-time and
# --buffer-time, microseconds). A fault that comes and goes with these is a
# stream-position or period-timing fault (the HDA controller's late position
# reports, `bdl_pos_adj`), not the voice or the speaker.
TIMINGS = (("small", 5000, 20000), ("default", None, None), ("large", 100000, 400000))
# What each device plays: a steady tone (dropouts are measurable) and a
# speech-like clip (what chopping sounds like). hw:0,0 takes only native formats.
TIMING_PLAN = ((PLAY_DEVICE, "tone1k-12-16k"), (PLAY_DEVICE, "speech-16k"), (PLAY_DEVICE, "tone1k-12-22k"),
               (CAPTURE_DEVICE, "tone1k-12-48k"), (CAPTURE_DEVICE, "speech-48k"))


def _timing_row(host, report, files, setting, period_us, buffer_us, device, name, listen):
    path, kind = files[name]
    before = _bdl_count(host)
    gaps, recorded = None, None
    if listen:
        result = loopback(host, report, path, f"timing-{setting}-{'plug' if device == PLAY_DEVICE else 'hw'}-{name}",
                          device=device, period_us=period_us, buffer_us=buffer_us)
        if result is not None:
            rate, channels, played = result
            recorded = True
            if kind == "tone":
                best = tone_channel(channels, rate)
                offset = onset_of(channels[best], rate)
                if offset is not None:  # heard: count the gaps inside the steady tone
                    gaps = audio.dropouts(channels[best], rate, start=offset + 0.2,
                                          end=offset + audio.wav_seconds(path) - 0.2)["count"]
        else:
            played = play(host, path, device, period_us=period_us, buffer_us=buffer_us)
    else:
        played = play(host, path, device, period_us=period_us, buffer_us=buffer_us)
    after = _bdl_count(host)
    params = played.get("hw_params") or {}
    status, evidence, _ = judge_play(played)
    return {"setting": setting, "device": device, "stimulus": name, "asked_period_us": period_us,
            "asked_buffer_us": buffer_us, "period_size": params.get("period_size"),
            "buffer_size": params.get("buffer_size"), "rate": hw_rate(params), "code": played.get("code"),
            "underruns": played.get("underruns"), "pointer_ratio": played.get("pointer_ratio"),
            "wav_s": played.get("wav_s"), "wall_s": played.get("wall_s"),
            "bdl_pos_adj": None if before is None or after is None else after - before,
            "dropouts": gaps, "recorded": recorded, "status": status, "evidence": evidence,
            "error": played.get("error")}


def _row_faults(row):
    """What went wrong in one timed playback (empty when it played cleanly)."""
    faults = []
    if row["status"] == "fail" and row["device"] == PLAY_DEVICE:
        faults.append("failed" if row.get("error") or row.get("code") else "wrong speed")
    if row.get("underruns"):
        faults.append(f"{row['underruns']} underruns")
    if row.get("dropouts"):
        faults.append(f"{row['dropouts']} dropouts")
    if row.get("bdl_pos_adj"):
        faults.append(f"{row['bdl_pos_adj']} bdl_pos_adj notes")
    ratio = row.get("pointer_ratio")
    if ratio is not None and abs(ratio - 1.0) > RATE_TOLERANCE and "wrong speed" not in faults:
        faults.append(f"pointer rate {ratio}")
    return faults


def check_speaker_timing(host, report, files=None, *, level_dbfs=-20.0, timings=TIMINGS, plan=TIMING_PLAN):
    """The same tones and speech-like clips at small, default and large period and buffer sizes.

    Each playback reports underruns, the pointer rate and new bdl_pos_adj
    kernel notes; with arecord the tones (16 kHz, the natural voice's
    22050 Hz, and 48 kHz on hw:0,0) are also recorded and their dropouts
    counted, and every recording is kept (Pip's own known signals)."""
    if host.which("aplay") is None:
        return [check("speaker.timing", "speaker", "skip", None, "aplay is not installed")]
    files = files or speaker_stimuli(report, level_dbfs, {name for _, name in plan})
    listen = host.which("arecord") is not None
    rows = []
    for setting, period_us, buffer_us in timings:
        for device, name in plan:
            if name in files:
                rows.append(_timing_row(host, report, files, setting, period_us, buffer_us, device, name, listen))
    if not rows:
        return [check("speaker.timing", "speaker", "skip", None, "no timing stimuli")]
    report.write_text("speaker-timing.json", json.dumps(rows, indent=1))
    checks, faulty = [], {}
    for setting, period_us, buffer_us in timings:
        mine = [row for row in rows if row["setting"] == setting]
        faults = {f"{row['stimulus']} on {row['device']}": _row_faults(row) for row in mine}
        faults = {key: value for key, value in faults.items() if value}
        faulty[setting] = bool(faults)
        sizes = sorted({f"period {row['period_size']} / buffer {row['buffer_size']} frames"
                        for row in mine if row.get("period_size")})
        asked = "the driver's default" if period_us is None else f"{period_us} us periods, {buffer_us} us buffer"
        evidence = f"{setting} ({asked}; got {', '.join(sizes) or 'unknown sizes'}): " + (
            "; ".join(f"{key}: {', '.join(value)}" for key, value in faults.items()) if faults else
            f"{len(mine)} playbacks clean")
        checks.append(check(f"speaker.timing.{setting}", "speaker", "warn" if faults else "pass",
                            [{key: row[key] for key in row if key not in ("evidence",)} for row in mine],
                            evidence, "Playback misbehaved at this period size." if faults else ""))
    bad = [setting for setting, value in faulty.items() if value]
    good = [setting for setting, value in faulty.items() if not value]
    if not bad:
        summary = ("pass", "every period and buffer size played cleanly",
                   "No period-timing fault: if the voice still sounds garbled, look at the voice or the speaker.")
    elif good:
        summary = ("warn", f"faults at {', '.join(bad)} periods only; clean at {', '.join(good)}",
                   "The fault depends on period and buffer size: a stream-position or period-timing fault "
                   "(see bdl_pos_adj in dmesg), not the voice. Try the clean size, or snd_hda_intel.position_fix.")
    else:
        summary = ("warn", "faults at every period and buffer size",
                   "The fault does not depend on period size: look at the speaker path or the voice itself.")
    checks.append(check("speaker.timing", "speaker", summary[0], {"faulty": bad, "clean": good},
                        summary[1], summary[2]))
    return checks


# The acoustic loopback ----------------------------------------------------------------

def loopback(host, report, stimulus, name, *, extra=1.0, keep=True, **play_options):
    """Record while playing a known stimulus; (rate, channels, the playback) of the recording, or None.

    `play_options` go to play() (device, period_us, buffer_us). A kept
    recording is saved as the louder channel alone (the per-channel levels
    are in the checks' values), which halves what each run puts on the stick.
    With keep=False the recording is made in RAM and removed after reading."""
    seconds = audio.wav_seconds(stimulus) + PREROLL_S + extra
    # Not kept means the room alone (a floor): recorded in RAM, never on the stick.
    path = report.path(f"loop-{name}.wav") if keep else scratch_path(host, f"loop-{name}.wav")
    device = CAPTURE_DEVICE
    recorder = host.spawn(record_argv(path, device, 48000, 2, seconds))
    if recorder is None:
        return None
    host.sleep(PREROLL_S)
    if recorder.poll() is not None:  # hw:0,0 refused 48 kHz: the plug device converts
        device = "plughw:0,0"
        recorder = host.spawn(record_argv(path, device, 48000, 2, seconds))
        if recorder is None:
            return None
        host.sleep(PREROLL_S)
    played = play(host, stimulus, **play_options)
    try:
        recorder.wait(timeout=seconds + 15)
    except Exception:
        recorder.kill()
        if not keep:
            _remove(path)
        return None
    try:
        rate, channels = audio.read_wav(path)
    except (OSError, EOFError, ValueError):
        if not keep:
            _remove(path)
        return None
    if not keep:
        _remove(path)
    else:
        if len(channels) > 1:
            try:
                audio.write_wav(path, [max(channels, key=audio.rms)], rate)
            except OSError:
                pass
        report.note_file(path)
    return rate, channels, played


def _trace_wait(host, child, seconds, authorized, *, allow_exit=False):
    """Wait while both the child and live audio authorization remain valid."""
    deadline = host.monotonic() + seconds
    while host.monotonic() < deadline:
        if authorized is not None and not authorized():
            return False
        if child.poll() is not None:
            return allow_exit and child.returncode == 0
        host.sleep(min(0.1, max(0.0, deadline - host.monotonic())))
    code = child.poll()
    return (code is None or (allow_exit and code == 0)) and (authorized is None or authorized())


def capture_echo_trace(host, report, *, level_dbfs=-20.0, authorized=None):
    """Acquire one bounded host-scheduled reference/microphone pair in RAM.

    This is deliberately not called native duplex evidence: arecord and aplay
    are separate processes and their request timestamps are not ALSA hardware
    timestamps. The offline alignment tool must estimate their acoustic lag.
    """
    if host.which("arecord") is None or host.which("aplay") is None:
        return check("echo.trace", "echo", "skip", None, "arecord or aplay is missing")
    files = speaker_stimuli(report, min(level_dbfs, -12.0), {"clicks-48k"})
    stimulus = files["clicks-48k"][0]
    with tempfile.TemporaryFile(dir=report.folder) as room_audio:
        return _record_echo_trace(host, report, stimulus, room_audio, authorized)


def capture_native_echo_trace(host, report, session, *, level_dbfs=-20.0, authorized=None):
    """Acquire a bounded trace from one ALSA-linked capture/playback group.

    The helper refuses unsupported exact rate/period/timestamp/link settings.
    Python retains the privacy authority and promptly terminates it if the lid,
    heartbeat or safety latch changes during acquisition.
    """
    if not host.exists(str(NATIVE_DUPLEX)):
        return check("echo.native", "echo", "skip", None, "the native duplex helper is not installed")
    paths = tuple(report.folder / name for name in
                  ("echo-mic.wav", "echo-reference.wav", "echo-events.json"))
    if authorized is not None and not authorized():
        return check("echo.native", "echo", "skip", None, "authorization ended before native capture")
    child = None
    sealed = False
    try:
        child = session.track(host.spawn([str(host.path(str(NATIVE_DUPLEX))), "--out", str(report.folder),
                                          f"{level_dbfs:.1f}"]))
        if child is None:
            return check("echo.native", "echo", "skip", None, "the native duplex helper could not start")
        valid = True
        deadline = host.monotonic() + 7.0
        while child.poll() is None and host.monotonic() < deadline:
            if authorized is not None and not authorized():
                valid = False
                child.terminate()
                break
            host.sleep(0.1)
        if child.poll() is None:
            valid = False
            child.terminate()
        try:
            child.wait(timeout=2)
        except Exception:
            child.kill()
            child.wait(timeout=2)
        valid = valid and child.returncode == 0 and (authorized is None or authorized())
        if valid:
            valid = all(path.is_file() for path in paths)
        if not valid:
            return check("echo.native", "echo", "skip", None,
                         "native capture was revoked, timed out, or the card cannot provide an exact linked stream")
        for path in paths:
            report.note_file(path)
        sealed = True
        value = {"rate": 48000, "mic": paths[0].name, "reference": paths[1].name,
                 "events": paths[2].name, "timing_basis": "alsa-linked-trigger-monotonic-raw"}
        return check("echo.native", "echo", "pass", value,
                     "private RAM trace acquired from one ALSA-linked duplex group",
                     "This is measurement evidence only; production remains half duplex.")
    except (OSError, ValueError) as error:
        return check("echo.native", "echo", "warn", None, f"native capture cleanup was required: {error}")
    finally:
        if child is not None and child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=2)
            except Exception:
                try:
                    child.kill()
                    child.wait(timeout=2)
                except Exception:
                    pass
        if not sealed:
            for path in paths:
                _remove(path)


def _record_echo_trace(host, report, stimulus, room_audio, authorized):
    mic_path = report.folder / "echo-mic.wav"
    reference_path = report.folder / "echo-reference.wav"
    events_path = report.folder / "echo-events.json"
    paths = (mic_path, reference_path, events_path)
    duration = audio.wav_seconds(stimulus) + PREROLL_S + 1.0
    if authorized is not None and not authorized():
        return check("echo.trace", "echo", "skip", None, "authorization ended before capture")
    recorder_started = host.monotonic()
    recorder = host.spawn(record_argv("-", CAPTURE_DEVICE, 48000, 2, duration), stdout=room_audio)
    if recorder is None:
        return check("echo.trace", "echo", "skip", None, "the microphone recorder could not start")
    sealed = False
    try:
        if not _trace_wait(host, recorder, PREROLL_S, authorized):
            return check("echo.trace", "echo", "skip", None, "capture or authorization ended during preroll")
        playback_requested = host.monotonic()

        def overlap_authorized():
            return recorder.poll() is None and (authorized is None or authorized())

        played = play(host, stimulus, authorized=overlap_authorized)
        if played.get("code") != 0:
            return check("echo.trace", "echo", "skip", None, "capture, playback or authorization ended early")
        remaining = max(0.0, duration - (host.monotonic() - recorder_started))
        if (not _trace_wait(host, recorder, remaining + 1.0, authorized, allow_exit=True)
                or recorder.poll() != 0):
            return check("echo.trace", "echo", "skip", None, "capture or authorization ended during postroll")
        rate, mic_channels = audio.read_wav(f"/proc/self/fd/{room_audio.fileno()}")
        stimulus_rate, stimulus_channels = audio.read_wav(stimulus)
        if rate != 48000 or stimulus_rate != rate or not mic_channels or not stimulus_channels:
            raise ValueError("unexpected trace shape")
        microphone = max(mic_channels, key=audio.rms)
        offset = max(0, round((playback_requested - recorder_started) * rate))
        if offset >= len(microphone):
            raise ValueError("playback began after the recorded frames")
        reference = [0] * len(microphone)
        source = stimulus_channels[0]
        end = min(len(reference), offset + len(source))
        reference[offset:end] = source[:end - offset]
        audio.write_wav(mic_path, [microphone], rate)
        audio.write_wav(reference_path, [reference], rate)
        events = {
            "schema": 1, "name": "pip-diag-echo-trace", "frame_ms": 10,
            "max_lag_ms": 250, "hold_ms": 150, "residual_rms_threshold": 0.025,
            "playback": [{"start_ms": round(offset * 1000 / rate, 3),
                          "end_ms": round(end * 1000 / rate, 3)}],
            "interruptions": [], "timing_basis": "host_scheduled_unmatched",
        }
        events_path.write_text(json.dumps(events, separators=(",", ":")) + "\n", encoding="utf-8")
        if authorized is not None and not authorized():
            return check("echo.trace", "echo", "skip", None, "authorization ended before the trace was sealed")
        for path in paths:
            report.note_file(path)
        sealed = True
    except (OSError, EOFError, ValueError) as error:
        return check("echo.trace", "echo", "warn", None, f"the trace could not be prepared: {error}")
    finally:
        if not sealed:
            for path in paths:
                _remove(path)
        if recorder.poll() is None:
            recorder.kill()
            recorder.wait(timeout=5)
    value = {"rate": rate, "mic": mic_path.name, "reference": reference_path.name,
             "events": events_path.name, "timing_basis": "host_scheduled_unmatched",
             "playback": played}
    return check("echo.trace", "echo", "pass", value,
                 "private RAM trace acquired; playback/reference alignment is host-scheduled, not native duplex",
                 "Run lab/audio_echo_evidence.py offline; do not treat this as an AEC promotion measurement.")


def tone_channel(channels, rate, frequency=1000):
    """The channel that carries most of a test tone (not the noisiest one)."""
    def strength(index):
        channel = channels[index]
        middle = len(channel) // 2
        return audio.goertzel(channel[max(0, middle - rate // 2):middle + rate // 2], rate, frequency)

    return max(range(len(channels)), key=strength)


def onset_of(samples, rate, frequency=1000):
    """Where a generated tone starts (its fade's middle taken off), or None when it never stood out."""
    found = audio.tone_onset(samples, rate, frequency)
    return None if found is None else round(max(0.0, found - audio.TONE_FADE / 2), 4)


def floor_recording(host, report, files):
    """The room through the same path while the card plays digital silence: (rate, channels) or None."""
    if "silence-48k" not in files:
        return None
    result = loopback(host, report, files["silence-48k"][0], "silence-48k", extra=0.5, keep=False)
    if result is None:
        return None
    rate, channels, _ = result
    return rate, [channel[int(0.2 * rate):] for channel in channels]


def measure_tone(channels, rate, floor, *, seconds=3.0):
    """One recorded 1 kHz tone against the silence recording: level, heard, latency, THD, drift, gaps."""
    best = tone_channel(channels, rate)
    samples = channels[best]
    offset = onset_of(samples, rate)
    quiet = floor[best] if floor is not None and best < len(floor) else samples[:int(0.25 * rate)]
    start = (offset if offset is not None else PREROLL_S) + THD_START_S
    steady = samples[int(start * rate):int((start + THD_SECONDS) * rate)]
    distortion = audio.thd_against_floor(steady, quiet, rate, 1000)
    level, floor_db = audio.dbfs(audio.rms(steady)), audio.dbfs(audio.rms(quiet))
    rise = round(level - floor_db, 2)
    heard = rise >= HEARD_DB or (distortion["snr_db"] is not None and distortion["snr_db"] >= HEARD_DB)
    # The recording started PREROLL_S before aplay was asked to play: what is
    # left is the playback and capture path's delay (plus program start-up).
    # An onset before that is a capture that started late: the delay is unknown.
    latency = None if offset is None or offset < PREROLL_S else round(offset - PREROLL_S, 3)
    drift, gaps = None, {"count": 0, "at_s": [], "median_dbfs": None}
    if heard and offset is not None:
        drift = audio.drift_ppm(samples, rate, 1000, start=start, end=start + THD_SECONDS)
        gaps = audio.dropouts(samples, rate, start=offset + 0.2, end=offset + seconds - 0.2)
    return {"channel": best, "onset_s": offset, "latency_s": latency, "level_dbfs": level, "floor_dbfs": floor_db,
            "rise_db": rise, "bin_snr_db": distortion["snr_db"], "heard": heard, "thd": distortion,
            "drift_ppm": drift, "dropouts": gaps, "levels": audio.channel_levels(channels)}


def thd_text(row):
    distortion = row["thd"]
    if not distortion["measurable"]:
        return f"{row['stimulus']} not measurable ({distortion['reason']})"
    if distortion["below_floor"]:
        return f"{row['stimulus']} below the floor (< {distortion['limit_percent']}%)"
    return f"{row['stimulus']} {distortion['percent']}%"


def judge_thd(rows):
    """loopback.thd from the rows: distortion only where it stood above the room."""
    measured = [(row, row["thd"]) for row in rows if row["thd"]["measurable"]]
    value = [{"stimulus": row["stimulus"], "thd_percent": row["thd"]["percent"],
              "below_floor": row["thd"]["below_floor"], "limit_percent": row["thd"]["limit_percent"],
              "snr_db": row["thd"]["snr_db"], "harmonics": row["thd"]["harmonics"]} for row in rows]
    evidence = "THD at 1 kHz, harmonics counted only 6 dB above a silence recording: " + \
        ", ".join(thd_text(row) for row in rows)
    if not measured:
        return check("loopback.thd", "loopback", "skip", value, evidence,
                     "No tone stood far enough above the room floor to measure distortion.")
    # Below the floor counts as its limit for the trend (an upper bound, never a measurement).
    percents = [distortion["percent"] for _, distortion in measured if distortion["percent"] is not None]
    rising = len(percents) >= 2 and percents[-1] > 3 * max(percents[0], 0.5)
    top = percents[-1] if percents else None
    distorted = rising or (top is not None and top > THD_WARN_PERCENT)
    return check("loopback.thd", "loopback", "warn" if distorted else "pass", value, evidence,
                 "Distortion grows with level: the speaker or its amplifier distorts." if rising else
                 f"Over {THD_WARN_PERCENT:g}% distortion at the loudest tone." if distorted else "")


def check_loopback(host, report, *, level_dbfs=-20.0, files=None):
    """Play known stimuli and record them: level, latency, THD, bands, dropouts, the 22050 Hz path.

    Every figure is judged against a silence recording through the same
    path. A steady tone's onset comes from its own narrowband energy, never
    from cross-correlating a sine (which lines up at any period)."""
    if host.which("arecord") is None or host.which("aplay") is None:
        return [check("loopback.level", "loopback", "skip", None, "arecord or aplay is missing")]
    files = files or speaker_stimuli(report, level_dbfs)
    checks = []
    dmesg_before = _bdl_count(host)
    floor = floor_recording(host, report, files)
    floor_channels = floor[1] if floor is not None else None
    rows = []
    for name in ("tone1k-20-16k", "tone1k-12-16k", "tone1k-6-16k"):
        result = loopback(host, report, files[name][0], name)
        if result is None:
            continue
        rate, channels, _ = result
        rows.append({"stimulus": name, **measure_tone(channels, rate, floor_channels,
                                                       seconds=audio.wav_seconds(files[name][0]))})
    if not rows:
        return [check("loopback.level", "loopback", "warn", None, "no loopback recording could be made",
                      "The microphone or speaker could not be opened together.")]
    first = rows[0]
    side = ("left", "right")[first["channel"]] if first["channel"] < 2 else f"channel {first['channel'] + 1}"
    late = (f"about {first['latency_s']} s late" if first["latency_s"] is not None else "delay unknown")
    checks.append(check("loopback.level", "loopback", "pass" if first["heard"] else "fail",
                        {key: first[key] for key in ("levels", "channel", "level_dbfs", "floor_dbfs", "rise_db",
                                                     "bin_snr_db", "latency_s", "onset_s", "heard")}
                        | {"floor_measured": floor is not None},
                        f"a -20 dBFS 1 kHz tone came back at {first['level_dbfs']} dBFS on the {side}, "
                        f"{first['rise_db']} dB above the room floor ({first['bin_snr_db']} dB in its own bin), "
                        f"{late}",
                        "" if first["heard"] else "The microphone did not hear the speaker: one of them is not working."))
    checks.append(judge_thd(rows))
    gaps = sum(row["dropouts"]["count"] for row in rows)
    checks.append(check("loopback.dropouts", "loopback", "warn" if gaps else "pass", gaps,
                        f"{gaps} gaps of 20 ms in steady tones",
                        "The audio path drops out: a timing fault, not the voice." if gaps else ""))
    if "tone1k-12-22k" in files:
        checks.append(check_rate_22k(host, report, files, floor_channels))
    sweep = loopback(host, report, files["sweep-48k"][0], "sweep-48k") if "sweep-48k" in files else None
    if sweep is not None:
        rate, channels, _ = sweep
        _, reference = audio.read_wav(files["sweep-48k"][0])
        best = max(range(len(channels)), key=lambda index: audio.rms(channels[index]))
        # A broadband sweep at 48 kHz shares the capture's clock: a correlation lines it up.
        lag, _ = audio.latency(reference[0], channels[best], rate, max_lag_s=1.0)
        bands = audio.sweep_bands(channels[best], rate, 50, 7900, 6.0, offset=PREROLL_S if lag is None else lag)
        low = [bands[key] for key in ("63", "125") if key in bands]
        mid = [bands[key] for key in ("1000", "2000") if key in bands]
        weak_bass = low and mid and max(low) < min(mid) - 25
        checks.append(check("loopback.bands", "loopback", "pass", bands,
                            "octave bands of the swept sine: " + ", ".join(f"{k} Hz {v}" for k, v in bands.items()),
                            "Hardly any bass comes back: these speakers reproduce little below about 500 Hz."
                            if weak_bass else ""))
    dmesg_after = _bdl_count(host)
    if dmesg_before is not None and dmesg_after is not None:
        grew = dmesg_after - dmesg_before
        checks.append(check("loopback.irq_timing", "loopback", "warn" if grew else "pass", grew,
                            f"new 'bdl_pos_adj' kernel notes during playback: {grew}",
                            "The HDA controller's position reports are late." if grew else ""))
    return checks


def check_rate_22k(host, report, files, floor_channels):
    """The natural voice's 22050 Hz through plughw: what rate the card ran, the level, and the clock drift."""
    path = files["tone1k-12-22k"][0]
    result = loopback(host, report, path, "tone1k-12-22k")
    if result is None:
        return check("loopback.rate_22k", "loopback", "warn", None, "the 22050 Hz tone could not be recorded")
    rate, channels, played = result
    row = measure_tone(channels, rate, floor_channels, seconds=audio.wav_seconds(path))
    hw = hw_rate(played.get("hw_params") or {})
    value = {"hw_rate": hw, "drift_ppm": row["drift_ppm"], "level_dbfs": row["level_dbfs"], "rise_db": row["rise_db"],
             "heard": row["heard"], "underruns": played.get("underruns"), "chain": played.get("chain")}
    played_at = f"played at {hw} Hz" if hw else "hardware rate unknown"
    if not row["heard"]:
        return check("loopback.rate_22k", "loopback", "warn", value,
                     f"a 22050 Hz file ({played_at}) was not heard: {row['rise_db']} dB above the floor",
                     "The natural voice's rate does not come out of the speaker; compare speaker.play.*22k*.")
    drift = row["drift_ppm"]
    evidence = (f"a 22050 Hz file {played_at}, heard {row['rise_db']} dB above the floor; its clock runs "
                f"{'unknown' if drift is None else f'{drift:+g} ppm'} against the 48 kHz capture")
    if drift is not None and abs(drift) > DRIFT_WARN_PPM:
        return check("loopback.rate_22k", "loopback", "warn", value, evidence,
                     "The 44.1 kHz family runs far from its nominal rate: the voice plays slightly off pitch.")
    return check("loopback.rate_22k", "loopback", "pass", value, evidence)


def _bdl_count(host):
    result = host.run(["dmesg"], timeout=10)
    if result is None or result.code:
        return None
    return result.out.count("bdl_pos_adj")


# Speaker isolation, per band ----------------------------------------------------------

def band_name(low, high):
    return f"{low}-{high} Hz"


def pin_name(label):
    return {"speaker_only": "Speaker", "bass_only": "Bass Speaker", "both": "both pins"}[label]


def _set_pins(host, mutes):
    """The saved mixer back, then the given pins muted."""
    host.run(["alsactl", "restore", "-f", str(host.path(str(MIXER_STATE))), CARD], timeout=20)
    for name in mutes:
        host.run(["amixer", "-c", CARD, "-q", "sset", name, "mute"], timeout=10)


def _band_levels(channels, rate, bands, *, start=PREROLL_S + 0.2, seconds=ISOLATION_SECONDS - 0.4):
    """[{band: dBFS}] per channel inside the steady part of a band-noise recording."""
    first, last = int(start * rate), int((start + seconds) * rate)
    return [{band_name(low, high): audio.band_level(channel[first:last], rate, low, high, settle=0.0)
             for low, high in bands} for channel in channels]


def check_speaker_isolation(host, report, session, files):
    """Which speaker pin carries which band, and which of each pin's channels play.

    Both pins, Speaker alone and Bass Speaker alone each play three
    band-limited noises (300-1200, 1200-2400 and 2400-8000 Hz); each pin
    alone also plays a wide band on the left only and on the right only.
    Every level is compared with the room in the same band. A pin that
    carries a band nobody else does is never to be muted, so no hint here
    ever says to mute one."""
    if not session.save_mixer():
        return check("speaker.isolation", "speaker", "skip", None, "the mixer could not be saved")
    bands = [band for band in ISOLATION_BANDS if f"band{band[0]}-{band[1]}-48k" in files]
    if not bands:
        return check("speaker.isolation", "speaker", "skip", None, "no band stimuli")
    configs = (("both", ()), ("speaker_only", ("Bass Speaker",)), ("bass_only", ("Speaker",)))
    levels = {label: {} for label, _ in configs}
    sides = {}
    floor = None
    try:
        _set_pins(host, ())
        quiet = loopback(host, report, files["silence-48k"][0], "iso-silence", extra=0.5, keep=False) \
            if "silence-48k" in files else None
        if quiet is not None:
            floor = _band_levels(quiet[1], quiet[0], bands)
        for label, mutes in configs:
            _set_pins(host, mutes)
            for low, high in bands:
                result = loopback(host, report, files[f"band{low}-{high}-48k"][0], f"iso-{label}-{low}", extra=0.5)
                if result is not None:
                    rate, channels, _ = result
                    levels[label][band_name(low, high)] = _band_levels(channels, rate, [(low, high)])
            if label == "both":
                continue
            for side in ("left", "right"):
                name = f"band{WIDE_BAND[0]}-{WIDE_BAND[1]}-48k-{side}"
                if name not in files:
                    continue
                result = loopback(host, report, files[name][0], f"iso-{label}-{side}", extra=0.5)
                if result is not None:
                    rate, channels, _ = result
                    sides.setdefault(label, {})[side] = _band_levels(channels, rate, bands)
    finally:
        host.run(["alsactl", "restore", "-f", str(host.path(str(MIXER_STATE))), CARD], timeout=20)
    if not any(levels.values()):
        return check("speaker.isolation", "speaker", "skip", None, "no recordings")
    mic = _mic_channel(levels)
    return [_judge_pins(levels, floor, mic, bands), _judge_sides(sides, floor, mic, bands)]


def _mic_channel(levels):
    """The microphone channel that hears the speakers best (the right one on this Mac)."""
    totals = {}
    for per_band in levels.values():
        for rows in per_band.values():
            for index, row in enumerate(rows):
                totals[index] = totals.get(index, 0.0) + sum(10 ** (value / 10) for value in row.values())
    return max(totals, key=totals.get) if totals else 0


def _at(rows, index, band):
    if not rows:
        return None
    row = rows[index] if index < len(rows) else rows[0]
    return row.get(band)


def _judge_pins(levels, floor, mic, bands):
    table, notes, silent_pins = {}, [], []
    floors = {band_name(*band): _at(floor, mic, band_name(*band)) for band in bands}
    for band in map(lambda pair: band_name(*pair), bands):
        row = {label: _at(levels[label].get(band), mic, band) for label in levels}
        row["floor"] = floors[band]
        table[band] = row
        speaker, bass = row.get("speaker_only"), row.get("bass_only")
        if speaker is not None and bass is not None:
            if abs(speaker - bass) < 3:
                notes.append(f"{band}: both pins alike")
            else:
                louder, quieter = ("Speaker", "Bass Speaker") if speaker > bass else ("Bass Speaker", "Speaker")
                notes.append(f"{band}: {louder} {abs(speaker - bass):.1f} dB above {quieter}")
    for label in ("speaker_only", "bass_only"):
        heard = [table[band].get(label) for band in table]
        if all(value is not None for value in heard) and all(
                table[band]["floor"] is not None and table[band][label] < table[band]["floor"] + PLAYS_DB
                for band in table):
            silent_pins.append(pin_name(label))
    value = {"mic_channel": mic, "bands": table}
    evidence = "; ".join(notes) or "no single-pin recordings"
    if silent_pins:
        return check("speaker.isolation", "speaker", "warn", value,
                     f"{evidence}; {', '.join(silent_pins)} gives nothing above the room in any band",
                     f"{' and '.join(silent_pins)} does not play: check that pin's mixer control and amplifier. "
                     "Keep both pins on; each carries its own band.")
    return check("speaker.isolation", "speaker", "pass", value, evidence,
                 "Keep both pins on: each carries its own band.")


def _judge_sides(sides, floor, mic, bands):
    """Per pin: does the left or the right channel drive a speaker."""
    if not sides:
        return check("speaker.isolation.channels", "speaker", "skip", None, "no left-only or right-only recordings")
    names = [band_name(*band) for band in bands]
    floors = {band: _at(floor, mic, band) for band in names}
    value, notes, dead_pins = {}, [], []
    for label, per_side in sides.items():
        value[label] = {}
        playing = {}
        for side, rows in per_side.items():
            row = {band: _at(rows, mic, band) for band in names}
            value[label][side] = row
            above = [row[band] - floors[band] for band in names if row[band] is not None and floors[band] is not None]
            playing[side] = bool(above) and max(above) >= PLAYS_DB
        if playing and not any(playing.values()):
            dead_pins.append(pin_name(label))
        for side, plays in playing.items():
            if not plays and any(playing.values()):
                notes.append(f"{pin_name(label)}'s {side} channel is at the room floor")
        if all(playing.get(side) for side in ("left", "right")):
            left, right = value[label]["left"], value[label]["right"]
            gaps = [left[band] - right[band] for band in names if left[band] is not None and right[band] is not None]
            if gaps and max(abs(gap) for gap in gaps) >= 10:
                worst_gap = max(gaps, key=abs)
                notes.append(f"{pin_name(label)}'s {'right' if worst_gap > 0 else 'left'} channel is "
                             f"{abs(worst_gap):.1f} dB below its {'left' if worst_gap > 0 else 'right'}")
    value["floor"] = floors
    if dead_pins:
        return check("speaker.isolation.channels", "speaker", "warn", value,
                     "; ".join(notes + [f"neither channel of {', '.join(dead_pins)} plays"]),
                     "A pin with no working channel plays nothing; check it before blaming the voice.")
    return check("speaker.isolation.channels", "speaker", "pass", value,
                 "; ".join(notes) or "every pin plays on both channels",
                 "Mono reaches both channels, so a channel at the floor loses nothing." if notes else "")

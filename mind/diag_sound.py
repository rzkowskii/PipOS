"""The sound card under test: the voice set aside, recordings, playback, mixer.

`hw:0,0` is exclusive and the image has no dmix, so every section that opens
the card first stops the voice. `AudioSession` does that safely:

1. it writes the marker /run/being-diag/voice-stopped (what to put back);
2. it stops the voice and, for `hear`, installs the diagnostic-microphone
   drop-in;
3. on the way out (normally, on an exception, or on SIGINT, SIGTERM or
   SIGHUP from a dropped SSH session) `restore()` removes the drop-in, puts
   the mixer back, restarts the voice and removes the marker.

`pip-diag restore` runs the same `restore()` by hand, and the systemd-run
wrapper in mind/diag.py runs it as ExecStopPost, so even a killed run puts
the voice back.

Sections: mic floor (per channel, dead-channel detection), mixer (with the
kept-state override), the Mic Boost x Capture sweep, the speaker battery
(rate, underruns, the `aplay -v` chain) and the acoustic loopback (level,
latency, THD, octave bands, dropouts).
"""

import json
import os
import re
import signal
import time
from pathlib import Path

from . import diag_audio as audio
from .diag_report import RUN_DIR, Interrupted, check
from .diag_system import systemctl, unit_state

VOICE = "being-voice.service"
MARKER = RUN_DIR / "voice-stopped"
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
    if marker and marker.get("mixer_saved") and state.exists():
        result = host.run(["alsactl", "restore", "-f", str(state), CARD], timeout=20)
        done.append("restored the mixer" if result is not None and result.code == 0 else "could not restore the mixer")
    if marker and marker.get("dump_coef") is not None:
        host.write("/sys/module/snd_hda_codec/parameters/dump_coef", str(marker["dump_coef"]))
    if marker is not None or had_dropin:
        wanted = True if marker is None else marker.get("voice_was_active", True)
        used_dropin = had_dropin or bool(marker and marker.get("dropin"))
        if wanted:
            # A restart, not a start: a voice running with the drop-in's
            # environment must come back without it.
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
    """The voice stopped (or running on the diagnostic microphone) for a while.

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
        stale = read_marker(self.host)
        if stale is not None:
            self.inherited = True
            state["voice_was_active"] = state["voice_was_active"] or stale.get("voice_was_active", True)
            state["dropin"] = state["dropin"] or bool(stale.get("dropin"))
            if stale.get("mixer_saved") and self.host.path(str(MIXER_STATE)).exists():
                state["mixer_saved"] = True  # the earlier run's copy is the mixer as it really was
            if stale.get("dump_coef") is not None:
                state["dump_coef"] = stale["dump_coef"]
            state["inherited_from"] = stale.get("pid")
        return state

    def __enter__(self):
        self._install()
        try:
            self.state = self._initial_state()
            write_marker(self.host, self.state)
            systemctl(self.host, "stop", VOICE)
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
         buffer_us=None):
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
                f"Only one side hears. Use being.mic_channel=auto (or {'right' if side == 'left' else 'left'}); "
                "on this Mac the mbp81 codec fixup may bring the other side back.")
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
    for rate in (16000, 48000):
        name = f"speech-{rate // 1000}k"
        if wanted(name):
            voiced = audio.speech_like(3.0, rate, -12)
            add(name, [voiced, voiced], rate, "speech")
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
TIMING_PLAN = ((PLAY_DEVICE, "tone1k-12-16k"), (PLAY_DEVICE, "speech-16k"),
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
                file_rate, reference = audio.read_wav(path)
                reference = audio.resample(reference[0], file_rate, rate)
                best = max(range(len(channels)), key=lambda index: audio.rms(channels[index]))
                lag, score = audio.latency(reference, channels[best], rate, max_lag_s=1.0)
                offset = PREROLL_S if lag is None else lag
                if score is not None and score >= 0.3:
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
    """The same tone and speech-like clip at small, default and large period and buffer sizes.

    Each playback reports underruns, the pointer rate and new bdl_pos_adj
    kernel notes; with arecord the tone is also recorded and its dropouts
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

def loopback(host, report, stimulus, name, *, extra=1.0, **play_options):
    """Record while playing a known stimulus; (rate, channels, the playback) of the recording, or None.

    `play_options` go to play() (device, period_us, buffer_us)."""
    seconds = audio.wav_seconds(stimulus) + PREROLL_S + extra
    path = report.path(f"loop-{name}.wav")
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
        return None
    try:
        rate, channels = audio.read_wav(path)
    except (OSError, EOFError, ValueError):
        return None
    report.note_file(path)
    return rate, channels, played


def check_loopback(host, report, *, level_dbfs=-20.0, files=None):
    """Play known stimuli and record them: level, latency, THD, bands, dropouts."""
    if host.which("arecord") is None or host.which("aplay") is None:
        return [check("loopback.level", "loopback", "skip", None, "arecord or aplay is missing")]
    files = files or speaker_stimuli(report, level_dbfs)
    checks = []
    dmesg_before = _bdl_count(host)
    thd_rows = []
    for name in ("tone1k-20-16k", "tone1k-12-16k", "tone1k-6-16k"):
        result = loopback(host, report, files[name][0], name)
        if result is None:
            continue
        rate, channels, _ = result
        _, reference = audio.read_wav(files[name][0])
        reference = audio.resample(reference[0], 16000, rate)
        best = max(range(len(channels)), key=lambda index: audio.rms(channels[index]))
        lag, score = audio.latency(reference, channels[best], rate, max_lag_s=1.0)
        offset = lag if lag is not None else PREROLL_S
        segment = channels[best][int((offset + 0.5) * rate):int((offset + 2.5) * rate)]
        distortion = audio.thd(segment, rate, 1000)
        gaps = audio.dropouts(channels[best], rate, start=offset + 0.2, end=offset + 2.8)
        # The recording started PREROLL_S before aplay was asked to play: what is
        # left is the playback and capture path's delay (plus program start-up).
        latency = None if lag is None else round(lag - PREROLL_S, 3)
        thd_rows.append({"stimulus": name, "channel": best, "offset_s": lag, "latency_s": latency, "match": score,
                         "levels": audio.channel_levels(channels), "thd": distortion, "dropouts": gaps})
    if not thd_rows:
        return [check("loopback.level", "loopback", "warn", None, "no loopback recording could be made",
                      "The microphone or speaker could not be opened together.")]
    first = thd_rows[0]
    heard = first["match"] >= 0.3
    levels = first["levels"]
    checks.append(check("loopback.level", "loopback", "pass" if heard else "fail",
                        {"levels": levels, "latency_s": first["latency_s"], "match": first["match"]},
                        f"a -20 dBFS 1 kHz tone came back at "
                        + ", ".join(f"{level['rms_dbfs']} dBFS" for level in levels)
                        + f", about {first['latency_s']} s late, match {first['match']}",
                        "" if heard else "The microphone did not hear the speaker: one of them is not working."))
    thd_values = [row["thd"]["percent"] for row in thd_rows if row["thd"]["percent"] is not None]
    rising = len(thd_values) >= 2 and thd_values[-1] > 3 * max(thd_values[0], 0.5)
    distorted = rising or bool(thd_values and thd_values[-1] > 10)
    checks.append(check("loopback.thd", "loopback", "warn" if distorted else "pass",
                        [{"stimulus": row["stimulus"], "thd_percent": row["thd"]["percent"]} for row in thd_rows],
                        "THD at 1 kHz: " + ", ".join(f"{row['stimulus']} {row['thd']['percent']}%" for row in thd_rows),
                        "Distortion grows with level: the speaker or its amplifier distorts." if rising else ""))
    gaps = sum(row["dropouts"]["count"] for row in thd_rows)
    checks.append(check("loopback.dropouts", "loopback", "warn" if gaps else "pass", gaps,
                        f"{gaps} gaps of 20 ms in steady tones",
                        "The audio path drops out: a timing fault, not the voice." if gaps else ""))
    sweep = loopback(host, report, files["sweep-48k"][0], "sweep-48k")
    if sweep is not None:
        rate, channels, _ = sweep
        _, reference = audio.read_wav(files["sweep-48k"][0])
        best = max(range(len(channels)), key=lambda index: audio.rms(channels[index]))
        lag, _ = audio.latency(reference[0], channels[best], rate, max_lag_s=1.0)
        bands = audio.sweep_bands(channels[best], rate, 50, 7900, 6.0, offset=PREROLL_S if lag is None else lag)
        low = [bands[key] for key in ("63", "125") if key in bands]
        mid = [bands[key] for key in ("1000", "2000") if key in bands]
        weak_bass = low and mid and max(low) < min(mid) - 25
        checks.append(check("loopback.bands", "loopback", "pass", bands,
                            "octave bands of the swept sine: " + ", ".join(f"{k} Hz {v}" for k, v in bands.items()),
                            "Hardly any bass comes back: the bass speaker may not be playing." if weak_bass else ""))
    dmesg_after = _bdl_count(host)
    if dmesg_before is not None and dmesg_after is not None:
        grew = dmesg_after - dmesg_before
        checks.append(check("loopback.irq_timing", "loopback", "warn" if grew else "pass", grew,
                            f"new 'bdl_pos_adj' kernel notes during playback: {grew}",
                            "The HDA controller's position reports are late." if grew else ""))
    return checks


def _bdl_count(host):
    result = host.run(["dmesg"], timeout=10)
    if result is None or result.code:
        return None
    return result.out.count("bdl_pos_adj")


def check_speaker_isolation(host, report, session, files):
    """Speaker only, Bass Speaker only, both: which one the microphone hears."""
    if not session.save_mixer():
        return check("speaker.isolation", "speaker", "skip", None, "the mixer could not be saved")
    rows = {}
    try:
        for label, mutes in (("both", ()), ("speaker_only", ("Bass Speaker",)), ("bass_only", ("Speaker",))):
            host.run(["alsactl", "restore", "-f", str(host.path(str(MIXER_STATE))), CARD], timeout=20)
            for name in mutes:
                host.run(["amixer", "-c", CARD, "-q", "sset", name, "mute"], timeout=10)
            result = loopback(host, report, files["tone1k-12-16k"][0], f"iso-{label}", extra=0.5)
            if result is not None:
                rate, channels, _ = result
                rows[label] = max(audio.dbfs(audio.goertzel(channel[int(0.6 * rate):int(2.6 * rate)], rate, 1000))
                                  for channel in channels)
    finally:
        host.run(["alsactl", "restore", "-f", str(host.path(str(MIXER_STATE))), CARD], timeout=20)
    if not rows:
        return check("speaker.isolation", "speaker", "skip", None, "no recordings")
    silent = [label for label, level in rows.items() if label != "both" and level < rows.get("both", 0) - 20]
    return check("speaker.isolation", "speaker", "warn" if silent else "pass", rows,
                 ", ".join(f"{label} {level} dBFS at 1 kHz" for label, level in rows.items()),
                 f"Muting the other speaker leaves almost nothing: {', '.join(silent)} may be silent."
                 if silent else "")

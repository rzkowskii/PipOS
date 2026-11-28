"""The ctypes bindings in mind/sherpa.py, against a fake sherpa-onnx library.

`FakeLibrary` is shared with the recognizer, voice and bench tests. It acts on
the real ctypes structs the bindings build, so a field set in the wrong place
shows up here; the C layout itself is checked in test_sherpa_layout.py.
"""

import ctypes as c
import tempfile
import time
import unittest
import wave
from array import array
from pathlib import Path

from mind import sherpa


class Fn:
    """A callable that, like a ctypes function, accepts restype and argtypes."""

    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class FakeLibrary:
    """A stand-in for libsherpa-onnx-c-api.so.

    VAD: a 512-sample window whose mean absolute value is at least 0.05 is
    speech. Speech is "detected" after min_speech_duration of it, and a
    segment ends after min_silence_duration of quiet (or max_speech_duration).
    Recognition returns `transcript` after `delay` seconds, or raises `error`.
    TTS makes 0.1 s of audio per word at `tts_rate`, scaled by 1 / speed.
    """

    path = "fake"

    def __init__(self, version=b"v2.0.3", *, transcript="what time is it", delay=0.0, error=None,
                 tts_rate=22050, tts_seconds_per_word=0.1, omit=()):
        self.version = version
        self.transcript = transcript
        self.delay = delay
        self.error = error
        self.tts_rate = tts_rate
        self.tts_seconds_per_word = tts_seconds_per_word
        self.calls = []
        self.configs = {}
        self.vads = {}
        self.live = {}
        self.decodes = 0
        self.accept_rates = []
        self._next = 1000
        for name in sherpa.SIGNATURES:
            if name in omit:
                continue
            method = getattr(self, "_" + name.removeprefix("SherpaOnnx"))
            setattr(self, name, Fn(self._recorded(name, method)))

    def _recorded(self, name, method):
        def call(*args):
            self.calls.append(name)
            return method(*args)
        return call

    def _handle(self, kind, value=None):
        self._next += 1
        self.live[self._next] = (kind, value)
        return self._next

    def _drop(self, handle, kind):
        assert self.live.pop(handle)[0] == kind, f"wrong handle for {kind}"

    # Versions
    def _GetVersionStr(self):
        return self.version

    def _GetOnnxruntimeVersionStr(self):
        return b"1.28.2"

    # VAD
    def _CreateVoiceActivityDetector(self, config, seconds):
        cfg = config.contents
        self.configs["vad"] = {
            "model": cfg.silero_vad.model, "threshold": cfg.silero_vad.threshold,
            "min_silence": cfg.silero_vad.min_silence_duration, "min_speech": cfg.silero_vad.min_speech_duration,
            "window": cfg.silero_vad.window_size, "max_speech": cfg.silero_vad.max_speech_duration,
            "rate": cfg.sample_rate, "threads": cfg.num_threads, "provider": cfg.provider, "buffer": seconds,
        }
        handle = self._handle("vad")
        self.vads[handle] = {"pending": array("f"), "speech": array("f"), "speaking": 0, "quiet": 0,
                             "detected": False, "segments": [], "position": 0, "start": 0,
                             "accepted": 0, "config": dict(self.configs["vad"])}
        return handle

    def _DestroyVoiceActivityDetector(self, handle):
        self._drop(handle, "vad")
        self.vads.pop(handle)

    def _VoiceActivityDetectorAcceptWaveform(self, handle, buffer, count):
        state = self.vads[handle]
        config = state["config"]
        state["accepted"] += count
        state["pending"].extend(buffer[:count])
        window = config["window"]
        rate = config["rate"]
        while len(state["pending"]) >= window:
            chunk = state["pending"][:window]
            del state["pending"][:window]
            loud = sum(abs(x) for x in chunk) / window >= 0.05
            if loud:
                if not state["speech"]:
                    state["start"] = state["position"]
                state["speech"].extend(chunk)
                state["speaking"] += window
                state["quiet"] = 0
                if state["speaking"] >= config["min_speech"] * rate:
                    state["detected"] = True
            elif state["speech"]:
                state["speech"].extend(chunk)
                state["quiet"] += window
                if not state["detected"] and state["quiet"] >= config["min_silence"] * rate:
                    state["speech"], state["speaking"], state["quiet"] = array("f"), 0, 0
            state["position"] += window
            too_long = len(state["speech"]) >= config["max_speech"] * rate
            if state["detected"] and (state["quiet"] >= config["min_silence"] * rate or too_long):
                state["segments"].append((state["start"], state["speech"]))
                state["speech"], state["speaking"], state["quiet"], state["detected"] = array("f"), 0, 0, False

    def _VoiceActivityDetectorEmpty(self, handle):
        return 0 if self.vads[handle]["segments"] else 1

    def _VoiceActivityDetectorDetected(self, handle):
        return 1 if self.vads[handle]["detected"] else 0

    def _VoiceActivityDetectorPop(self, handle):
        self.vads[handle]["segments"].pop(0)

    def _VoiceActivityDetectorClear(self, handle):
        self.vads[handle]["segments"].clear()

    def _VoiceActivityDetectorFront(self, handle):
        start, samples = self.vads[handle]["segments"][0]
        buffer = (c.c_float * len(samples))(*samples)
        segment = sherpa.SpeechSegment(start, c.cast(buffer, c.POINTER(c.c_float)), len(samples))
        pointer = c.pointer(segment)
        self.live[c.addressof(segment)] = ("segment", (segment, buffer))
        return pointer

    def _DestroySpeechSegment(self, pointer):
        self._drop(c.addressof(pointer.contents), "segment")

    def _VoiceActivityDetectorReset(self, handle):
        state = self.vads[handle]
        state.update(pending=array("f"), speech=array("f"), speaking=0, quiet=0, detected=False, segments=[])

    def _VoiceActivityDetectorFlush(self, handle):
        state = self.vads[handle]
        if state["detected"]:
            state["segments"].append((state["start"], state["speech"]))
        state.update(speech=array("f"), speaking=0, quiet=0, detected=False)

    # Offline recognizer
    def _CreateOfflineRecognizer(self, config):
        cfg = config.contents
        model = cfg.model_config
        self.configs["asr"] = {
            "encoder": model.moonshine.encoder, "merged_decoder": model.moonshine.merged_decoder,
            "preprocessor": model.moonshine.preprocessor, "cached_decoder": model.moonshine.cached_decoder,
            "tokens": model.tokens, "threads": model.num_threads, "provider": model.provider,
            "rate": cfg.feat_config.sample_rate, "decoding": cfg.decoding_method,
        }
        return self._handle("recognizer")

    def _DestroyOfflineRecognizer(self, handle):
        self._drop(handle, "recognizer")

    def _CreateOfflineStream(self, recognizer):
        assert self.live[recognizer][0] == "recognizer"
        return self._handle("stream", {"samples": 0})

    def _DestroyOfflineStream(self, handle):
        self._drop(handle, "stream")

    def _AcceptWaveformOffline(self, stream, rate, buffer, count):
        # The real recognizer resamples any rate to its 16 kHz features.
        assert 8000 <= rate <= 48000, rate
        self.accept_rates.append(rate)
        self.live[stream][1]["samples"] += count

    def _DecodeOfflineStream(self, recognizer, stream):
        self.decodes += 1
        if self.delay:
            time.sleep(self.delay)

    def _GetOfflineStreamResult(self, stream):
        if self.error:
            raise self.error
        result = sherpa.OfflineRecognizerResult()
        result.text = self.transcript.encode()
        pointer = c.pointer(result)
        self.live[c.addressof(result)] = ("result", result)
        return pointer

    def _DestroyOfflineRecognizerResult(self, pointer):
        self._drop(c.addressof(pointer.contents), "result")

    # TTS
    def _CreateOfflineTts(self, config):
        cfg = config.contents
        vits = cfg.model.vits
        self.configs["tts"] = {"model": vits.model, "tokens": vits.tokens, "data_dir": vits.data_dir,
                               "length_scale": vits.length_scale, "threads": cfg.model.num_threads,
                               "provider": cfg.model.provider}
        return self._handle("tts")

    def _DestroyOfflineTts(self, handle):
        self._drop(handle, "tts")

    def _OfflineTtsSampleRate(self, handle):
        return self.tts_rate

    def _OfflineTtsGenerateWithConfig(self, handle, text, options, callback, argument):
        speed = options.contents.speed
        self.configs.setdefault("speeds", []).append(round(speed, 4))
        if self.error:
            return None
        cfg = options.contents
        if cfg.phoneme_codepoints_num_sentences > 0:
            # Pre-phonemized input wins over the text, as in sherpa-onnx 2.x.
            lengths = cfg.phoneme_codepoints_lens[:cfg.phoneme_codepoints_num_sentences]
            spoken = "".join(map(chr, cfg.phoneme_codepoints[:sum(lengths)]))
            self.configs.setdefault("phonemes", []).append((text, lengths, spoken))
            text = spoken.encode()
        words = len(text.split())
        count = int(self.tts_rate * self.tts_seconds_per_word * words / speed)
        if self.delay:
            time.sleep(self.delay)
        buffer = (c.c_float * count)(*([0.25] * count))
        audio = sherpa.GeneratedAudio(c.cast(buffer, c.POINTER(c.c_float)), count, self.tts_rate)
        self.live[c.addressof(audio)] = ("audio", (audio, buffer))
        return c.pointer(audio)

    def _DestroyOfflineTtsGeneratedAudio(self, pointer):
        self._drop(c.addressof(pointer.contents), "audio")

    def _WriteWave(self, samples, count, rate, path):
        data = array("h", (int(samples[i] * 32767) for i in range(count)))
        with wave.open(path.decode(), "wb") as out:
            out.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            out.writeframes(data.tobytes())
        return 1


def loud(seconds, rate=16000, level=0.3):
    count = int(seconds * rate)
    return array("f", (level if i % 2 else -level for i in range(count)))


def quiet(seconds, rate=16000):
    return array("f", bytes(4 * int(seconds * rate)))


class LoadTests(unittest.TestCase):
    def test_a_missing_library_is_unavailable_with_its_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "libsherpa-onnx-c-api.so"
            with self.assertRaises(sherpa.Unavailable) as caught:
                sherpa.load(path)
        self.assertIn(str(path), caught.exception.reason)

    def test_a_file_that_is_not_a_library_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "libsherpa-onnx-c-api.so"
            path.write_text("not a library")
            with self.assertRaises(sherpa.Unavailable) as caught:
                sherpa.load(path)
        self.assertIn("would not load", caught.exception.reason)

    def test_unchecked_release_is_refused(self):
        with self.assertRaises(sherpa.Unavailable) as caught:
            sherpa.load(FakeLibrary(version=b"2.1.0"))
        self.assertIn("layout checked", caught.exception.reason)

    def test_both_checked_releases_load_and_report_versions(self):
        for version in (b"2.0.3", b"v2.0.2"):
            self.assertEqual(sherpa.version(FakeLibrary(version=version))["onnxruntime"], "1.28.2")

    def test_a_missing_symbol_is_unavailable(self):
        with self.assertRaises(sherpa.Unavailable) as caught:
            sherpa.load(FakeLibrary(omit={"SherpaOnnxOfflineTtsGenerateWithConfig"}))
        self.assertIn("SherpaOnnxOfflineTtsGenerateWithConfig", caught.exception.reason)

    def test_signatures_are_set_on_the_functions(self):
        lib = sherpa.load(FakeLibrary())
        self.assertIs(lib.SherpaOnnxCreateOfflineTts.restype, c.c_void_p)
        self.assertEqual(lib.SherpaOnnxAcceptWaveformOffline.argtypes,
                         (c.c_void_p, c.c_int32, c.POINTER(c.c_float), c.c_int32))

    def test_pcm_conversion_scales_to_unit_range(self):
        pcm = array("h", [0, 16384, -32768, 32767]).tobytes()
        self.assertEqual(list(sherpa.pcm_to_float(pcm))[:3], [0.0, 0.5, -1.0])
        self.assertAlmostEqual(sherpa.pcm_to_float(pcm)[3], 32767 / 32768)


class VadTests(unittest.TestCase):
    def test_config_and_one_segment(self):
        fake = FakeLibrary()
        vad = sherpa.Vad("/models/asr/silero_vad.onnx", library=fake)
        self.assertEqual(fake.configs["vad"]["window"], 512)
        self.assertAlmostEqual(fake.configs["vad"]["threshold"], 0.5)
        self.assertAlmostEqual(fake.configs["vad"]["min_silence"], 0.3)
        self.assertAlmostEqual(fake.configs["vad"]["max_speech"], 15.0)
        self.assertEqual((fake.configs["vad"]["rate"], fake.configs["vad"]["threads"]), (16000, 1))
        self.assertEqual(fake.configs["vad"]["provider"], b"cpu")
        vad.accept(quiet(0.2))
        self.assertFalse(vad.detected())
        vad.accept(loud(0.5))
        self.assertTrue(vad.detected())
        self.assertEqual(vad.pop_segments(), [])
        vad.accept(quiet(0.4))
        segments = vad.pop_segments()
        self.assertEqual(len(segments), 1)
        self.assertGreater(len(segments[0][1]), 0.5 * 16000)
        vad.close()
        self.assertEqual(fake.live, {})

    def test_empty_input_is_a_no_op(self):
        fake = FakeLibrary()
        vad = sherpa.Vad("vad.onnx", library=fake)
        vad.accept(array("f"))
        self.assertNotIn("SherpaOnnxVoiceActivityDetectorAcceptWaveform", fake.calls)


class RecognizerTests(unittest.TestCase):
    def test_moonshine_v2_config_and_transcribe(self):
        fake = FakeLibrary(transcript=" set a timer for five minutes ")
        recognizer = sherpa.OfflineRecognizer("/m/encoder_model.ort", "/m/decoder_model_merged.ort",
                                              "/m/tokens.txt", library=fake)
        config = fake.configs["asr"]
        self.assertEqual((config["encoder"], config["merged_decoder"]),
                         (b"/m/encoder_model.ort", b"/m/decoder_model_merged.ort"))
        self.assertEqual((config["preprocessor"], config["cached_decoder"]), (None, None))
        self.assertEqual((config["threads"], config["provider"], config["decoding"]), (1, b"cpu", b"greedy_search"))
        self.assertEqual(recognizer.transcribe(loud(1.0)), "set a timer for five minutes")
        recognizer.close()
        self.assertEqual(fake.live, {})

    def test_a_failed_load_is_unavailable(self):
        fake = FakeLibrary()
        fake.SherpaOnnxCreateOfflineRecognizer = Fn(lambda config: None)
        with self.assertRaises(sherpa.Unavailable):
            sherpa.OfflineRecognizer("e", "d", "t", library=fake)


class TtsTests(unittest.TestCase):
    def test_write_makes_a_16_bit_wav_and_passes_the_speed(self):
        fake = FakeLibrary()
        tts = sherpa.OfflineTts("/v/en_GB-alba-medium.onnx", "/v/tokens.txt", "/v/espeak-ng-data", library=fake)
        self.assertEqual(tts.sample_rate, 22050)
        self.assertEqual(fake.configs["tts"]["threads"], 1)
        self.assertEqual(fake.configs["tts"]["length_scale"], 1.0)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "clause.wav"
            seconds = tts.write("one two three four", target, length_scale=0.8)
            with wave.open(str(target)) as audio:
                self.assertEqual((audio.getsampwidth(), audio.getnchannels(), audio.getframerate()), (2, 1, 22050))
                self.assertAlmostEqual(audio.getnframes() / 22050, seconds, places=3)
        self.assertEqual(fake.configs["speeds"], [1.25])
        samples, rate = tts.generate("hello", length_scale=1.0)
        self.assertEqual((len(samples), rate), (2205, 22050))
        tts.close()
        self.assertEqual(fake.live, {})

    def test_no_audio_raises(self):
        tts = sherpa.OfflineTts("m", "t", "d", library=FakeLibrary(error=RuntimeError("bad")))
        with self.assertRaises(RuntimeError):
            tts.generate("hello")


class WavTests(unittest.TestCase):
    def write(self, folder, rate, seconds=0.5, channels=1):
        path = Path(folder) / f"{rate}.wav"
        with wave.open(str(path), "wb") as out:
            out.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
            out.writeframes(array("h", [16384] * int(seconds * rate) * channels).tobytes())
        return path

    def test_reads_any_common_rate_and_refuses_stereo(self):
        with tempfile.TemporaryDirectory() as folder:
            samples, rate = sherpa.read_wav(self.write(folder, 24000))
            self.assertEqual((rate, len(samples)), (24000, 12000))
            self.assertAlmostEqual(samples[0], 0.5)
            with self.assertRaises(ValueError):
                sherpa.read_wav(self.write(folder, 16000, channels=2))

    def test_resample_keeps_duration_and_shape(self):
        ramp = array("f", (i / 24000 for i in range(24000)))
        out = sherpa.resample(ramp, 24000, 16000)
        self.assertEqual(len(out), 16000)
        self.assertAlmostEqual(out[8000], ramp[12000], places=5)
        self.assertIs(sherpa.resample(out, 16000, 16000), out)
        self.assertEqual(len(sherpa.resample(array("f"), 24000)), 0)


if __name__ == "__main__":
    unittest.main()

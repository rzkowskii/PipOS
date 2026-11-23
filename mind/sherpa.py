"""ctypes bindings for the three parts of the sherpa-onnx C API the voice uses.

Only these are bound: Silero voice activity detection, the offline recognizer
(Moonshine v2: an encoder plus a merged decoder) and offline TTS (VITS, which
is how Piper voices run). The library is loaded lazily, on first use; when it
is missing or is a release whose struct layout was not checked, `Unavailable`
is raised with a plain reason and the voice keeps PocketSphinx and Flite.

The structs mirror `sherpa-onnx/c-api/c-api.h` of v2.0.3, which is identical
to v2.0.2. `lab/sherpa_layout_check.c` prints the C sizes and offsets and
`tests/test_sherpa_layout.py` compares them with the ones below, so a release
whose layout moved is caught on the host, not as a crash on the laptop.

Standard library only. ctypes releases the GIL during each foreign call, so a
decode in a worker thread does not stall the event loop.
"""

import ctypes as c
import os
import threading
from array import array
from pathlib import Path

LIBRARY = Path("/usr/lib/libsherpa-onnx-c-api.so")
# Releases whose c-api.h layout matches the structs below (checked by the
# layout test against both downloaded headers).
CHECKED_VERSIONS = ("2.0.3", "2.0.2")
RATE = 16000
PCM_SCALE = 1 / 32768


class Unavailable(RuntimeError):
    """sherpa-onnx cannot be used here. `reason` says why, in plain words."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


_P = c.c_char_p
_I = c.c_int32
_F = c.c_float
_FP = c.POINTER(c.c_float)


def _struct(name, fields):
    return type(name, (c.Structure,), {"_fields_": fields})


# Voice activity detection --------------------------------------------------------

_VAD_FIELDS = [("model", _P), ("threshold", _F), ("min_silence_duration", _F),
               ("min_speech_duration", _F), ("window_size", _I), ("max_speech_duration", _F)]
SileroVadModelConfig = _struct("SileroVadModelConfig", list(_VAD_FIELDS))
TenVadModelConfig = _struct("TenVadModelConfig", list(_VAD_FIELDS))
VadModelConfig = _struct("VadModelConfig", [
    ("silero_vad", SileroVadModelConfig), ("sample_rate", _I), ("num_threads", _I), ("provider", _P),
    ("debug", _I), ("ten_vad", TenVadModelConfig),
])
SpeechSegment = _struct("SpeechSegment", [("start", c.c_int64), ("samples", _FP), ("n", _I)])

# Offline recognition ---------------------------------------------------------------

FeatureConfig = _struct("FeatureConfig", [("sample_rate", _I), ("feature_dim", _I)])
OfflineTransducerModelConfig = _struct("OfflineTransducerModelConfig",
                                       [("encoder", _P), ("decoder", _P), ("joiner", _P)])
OfflineParaformerModelConfig = _struct("OfflineParaformerModelConfig", [("model", _P)])
OfflineNemoEncDecCtcModelConfig = _struct("OfflineNemoEncDecCtcModelConfig", [("model", _P)])
OfflineWhisperModelConfig = _struct("OfflineWhisperModelConfig", [
    ("encoder", _P), ("decoder", _P), ("language", _P), ("task", _P), ("tail_paddings", _I),
    ("enable_token_timestamps", _I), ("enable_segment_timestamps", _I),
])
OfflineCanaryModelConfig = _struct("OfflineCanaryModelConfig", [
    ("encoder", _P), ("decoder", _P), ("src_lang", _P), ("tgt_lang", _P), ("use_pnc", _I),
])
OfflineCohereTranscribeModelConfig = _struct("OfflineCohereTranscribeModelConfig", [
    ("encoder", _P), ("decoder", _P), ("language", _P), ("use_punct", _I), ("use_itn", _I),
])
OfflineFireRedAsrModelConfig = _struct("OfflineFireRedAsrModelConfig", [("encoder", _P), ("decoder", _P)])
OfflineFireRedAsrCtcModelConfig = _struct("OfflineFireRedAsrCtcModelConfig", [("model", _P)])
OfflineMoonshineModelConfig = _struct("OfflineMoonshineModelConfig", [
    ("preprocessor", _P), ("encoder", _P), ("uncached_decoder", _P), ("cached_decoder", _P),
    ("merged_decoder", _P),
])
OfflineTdnnModelConfig = _struct("OfflineTdnnModelConfig", [("model", _P)])
OfflineLMConfig = _struct("OfflineLMConfig", [("model", _P), ("scale", _F)])
OfflineSenseVoiceModelConfig = _struct("OfflineSenseVoiceModelConfig",
                                       [("model", _P), ("language", _P), ("use_itn", _I)])
OfflineDolphinModelConfig = _struct("OfflineDolphinModelConfig", [("model", _P)])
OfflineZipformerCtcModelConfig = _struct("OfflineZipformerCtcModelConfig", [("model", _P)])
OfflineWenetCtcModelConfig = _struct("OfflineWenetCtcModelConfig", [("model", _P)])
OfflineOmnilingualAsrCtcModelConfig = _struct("OfflineOmnilingualAsrCtcModelConfig", [("model", _P)])
OfflineFunASRNanoModelConfig = _struct("OfflineFunASRNanoModelConfig", [
    ("encoder_adaptor", _P), ("llm", _P), ("embedding", _P), ("tokenizer", _P), ("system_prompt", _P),
    ("user_prompt", _P), ("max_new_tokens", _I), ("temperature", _F), ("top_p", _F), ("seed", _I),
    ("language", _P), ("itn", _I), ("hotwords", _P),
])
OfflineQwen3ASRModelConfig = _struct("OfflineQwen3ASRModelConfig", [
    ("conv_frontend", _P), ("encoder", _P), ("decoder", _P), ("tokenizer", _P), ("max_total_len", _I),
    ("max_new_tokens", _I), ("temperature", _F), ("top_p", _F), ("seed", _I), ("hotwords", _P),
])
OfflineMedAsrCtcModelConfig = _struct("OfflineMedAsrCtcModelConfig", [("model", _P)])
OfflineModelConfig = _struct("OfflineModelConfig", [
    ("transducer", OfflineTransducerModelConfig), ("paraformer", OfflineParaformerModelConfig),
    ("nemo_ctc", OfflineNemoEncDecCtcModelConfig), ("whisper", OfflineWhisperModelConfig),
    ("tdnn", OfflineTdnnModelConfig), ("tokens", _P), ("num_threads", _I), ("debug", _I), ("provider", _P),
    ("model_type", _P), ("modeling_unit", _P), ("bpe_vocab", _P), ("telespeech_ctc", _P),
    ("sense_voice", OfflineSenseVoiceModelConfig), ("moonshine", OfflineMoonshineModelConfig),
    ("fire_red_asr", OfflineFireRedAsrModelConfig), ("dolphin", OfflineDolphinModelConfig),
    ("zipformer_ctc", OfflineZipformerCtcModelConfig), ("canary", OfflineCanaryModelConfig),
    ("wenet_ctc", OfflineWenetCtcModelConfig), ("omnilingual", OfflineOmnilingualAsrCtcModelConfig),
    ("medasr", OfflineMedAsrCtcModelConfig), ("funasr_nano", OfflineFunASRNanoModelConfig),
    ("fire_red_asr_ctc", OfflineFireRedAsrCtcModelConfig), ("qwen3_asr", OfflineQwen3ASRModelConfig),
    ("cohere_transcribe", OfflineCohereTranscribeModelConfig),
])
HomophoneReplacerConfig = _struct("HomophoneReplacerConfig",
                                  [("dict_dir", _P), ("lexicon", _P), ("rule_fsts", _P)])
OfflineCtcFstDecoderConfig = _struct("OfflineCtcFstDecoderConfig", [("graph", _P), ("max_active", _I)])
OfflineRecognizerConfig = _struct("OfflineRecognizerConfig", [
    ("feat_config", FeatureConfig), ("model_config", OfflineModelConfig), ("lm_config", OfflineLMConfig),
    ("decoding_method", _P), ("max_active_paths", _I), ("hotwords_file", _P), ("hotwords_score", _F),
    ("rule_fsts", _P), ("rule_fars", _P), ("blank_penalty", _F), ("hr", HomophoneReplacerConfig),
    ("ctc_fst_decoder_config", OfflineCtcFstDecoderConfig),
])
OfflineRecognizerResult = _struct("OfflineRecognizerResult", [
    ("text", _P), ("timestamps", _FP), ("count", _I), ("tokens", _P), ("tokens_arr", c.POINTER(_P)),
    ("json", _P), ("lang", _P), ("emotion", _P), ("event", _P), ("durations", _FP), ("ys_log_probs", _FP),
    ("segment_timestamps", _FP), ("segment_durations", _FP), ("segment_texts", _P),
    ("segment_texts_arr", c.POINTER(_P)), ("segment_count", _I),
])

# Offline TTS --------------------------------------------------------------------

OfflineTtsVitsModelConfig = _struct("OfflineTtsVitsModelConfig", [
    ("model", _P), ("lexicon", _P), ("tokens", _P), ("data_dir", _P), ("noise_scale", _F),
    ("noise_scale_w", _F), ("length_scale", _F), ("dict_dir", _P),
])
OfflineTtsMatchaModelConfig = _struct("OfflineTtsMatchaModelConfig", [
    ("acoustic_model", _P), ("vocoder", _P), ("lexicon", _P), ("tokens", _P), ("data_dir", _P),
    ("noise_scale", _F), ("length_scale", _F), ("dict_dir", _P),
])
OfflineTtsKokoroModelConfig = _struct("OfflineTtsKokoroModelConfig", [
    ("model", _P), ("voices", _P), ("tokens", _P), ("data_dir", _P), ("length_scale", _F), ("dict_dir", _P),
    ("lexicon", _P), ("lang", _P),
])
OfflineTtsKittenModelConfig = _struct("OfflineTtsKittenModelConfig", [
    ("model", _P), ("voices", _P), ("tokens", _P), ("data_dir", _P), ("length_scale", _F), ("lexicon", _P),
])
OfflineTtsZipvoiceModelConfig = _struct("OfflineTtsZipvoiceModelConfig", [
    ("tokens", _P), ("encoder", _P), ("decoder", _P), ("vocoder", _P), ("data_dir", _P), ("lexicon", _P),
    ("feat_scale", _F), ("t_shift", _F), ("target_rms", _F), ("guidance_scale", _F),
])
OfflineTtsPocketModelConfig = _struct("OfflineTtsPocketModelConfig", [
    ("lm_flow", _P), ("lm_main", _P), ("encoder", _P), ("decoder", _P), ("text_conditioner", _P),
    ("vocab_json", _P), ("token_scores_json", _P), ("voice_embedding_cache_capacity", _I),
])
OfflineTtsSupertonicModelConfig = _struct("OfflineTtsSupertonicModelConfig", [
    ("duration_predictor", _P), ("text_encoder", _P), ("vector_estimator", _P), ("vocoder", _P),
    ("tts_json", _P), ("unicode_indexer", _P), ("voice_style", _P),
])
OfflineTtsModelConfig = _struct("OfflineTtsModelConfig", [
    ("vits", OfflineTtsVitsModelConfig), ("num_threads", _I), ("debug", _I), ("provider", _P),
    ("matcha", OfflineTtsMatchaModelConfig), ("kokoro", OfflineTtsKokoroModelConfig),
    ("kitten", OfflineTtsKittenModelConfig), ("zipvoice", OfflineTtsZipvoiceModelConfig),
    ("pocket", OfflineTtsPocketModelConfig), ("supertonic", OfflineTtsSupertonicModelConfig),
])
OfflineTtsConfig = _struct("OfflineTtsConfig", [
    ("model", OfflineTtsModelConfig), ("rule_fsts", _P), ("max_num_sentences", _I), ("rule_fars", _P),
    ("silence_scale", _F),
])
GeneratedAudio = _struct("GeneratedAudio", [("samples", _FP), ("n", _I), ("sample_rate", _I)])
GenerationConfig = _struct("GenerationConfig", [
    ("silence_scale", _F), ("speed", _F), ("sid", _I), ("reference_audio", _FP), ("reference_audio_len", _I),
    ("reference_sample_rate", _I), ("reference_text", _P), ("num_steps", _I), ("extra", _P),
    ("phoneme_codepoints", c.POINTER(_I)), ("phoneme_codepoints_lens", c.POINTER(_I)),
    ("phoneme_codepoints_num_sentences", _I), ("tokens", c.POINTER(_P)), ("tokens_lens", c.POINTER(_I)),
    ("tokens_num_sentences", _I),
])

# Every struct above, by its C name without the SherpaOnnx prefix. The layout
# test walks this table.
STRUCTS = {cls.__name__: cls for cls in (
    SileroVadModelConfig, TenVadModelConfig, VadModelConfig, SpeechSegment, FeatureConfig,
    OfflineTransducerModelConfig, OfflineParaformerModelConfig, OfflineNemoEncDecCtcModelConfig,
    OfflineWhisperModelConfig, OfflineCanaryModelConfig, OfflineCohereTranscribeModelConfig,
    OfflineFireRedAsrModelConfig, OfflineFireRedAsrCtcModelConfig, OfflineMoonshineModelConfig,
    OfflineTdnnModelConfig, OfflineLMConfig, OfflineSenseVoiceModelConfig, OfflineDolphinModelConfig,
    OfflineZipformerCtcModelConfig, OfflineWenetCtcModelConfig, OfflineOmnilingualAsrCtcModelConfig,
    OfflineFunASRNanoModelConfig, OfflineQwen3ASRModelConfig, OfflineMedAsrCtcModelConfig,
    OfflineModelConfig, HomophoneReplacerConfig, OfflineCtcFstDecoderConfig, OfflineRecognizerConfig,
    OfflineRecognizerResult, OfflineTtsVitsModelConfig, OfflineTtsMatchaModelConfig,
    OfflineTtsKokoroModelConfig, OfflineTtsKittenModelConfig, OfflineTtsZipvoiceModelConfig,
    OfflineTtsPocketModelConfig, OfflineTtsSupertonicModelConfig, OfflineTtsModelConfig, OfflineTtsConfig,
    GeneratedAudio, GenerationConfig,
)}

_V = c.c_void_p
# name: (restype, argtypes)
SIGNATURES = {
    "SherpaOnnxGetVersionStr": (_P, ()),
    "SherpaOnnxGetOnnxruntimeVersionStr": (_P, ()),
    "SherpaOnnxCreateVoiceActivityDetector": (_V, (c.POINTER(VadModelConfig), _F)),
    "SherpaOnnxDestroyVoiceActivityDetector": (None, (_V,)),
    "SherpaOnnxVoiceActivityDetectorAcceptWaveform": (None, (_V, _FP, _I)),
    "SherpaOnnxVoiceActivityDetectorEmpty": (_I, (_V,)),
    "SherpaOnnxVoiceActivityDetectorDetected": (_I, (_V,)),
    "SherpaOnnxVoiceActivityDetectorPop": (None, (_V,)),
    "SherpaOnnxVoiceActivityDetectorClear": (None, (_V,)),
    "SherpaOnnxVoiceActivityDetectorFront": (c.POINTER(SpeechSegment), (_V,)),
    "SherpaOnnxDestroySpeechSegment": (None, (c.POINTER(SpeechSegment),)),
    "SherpaOnnxVoiceActivityDetectorReset": (None, (_V,)),
    "SherpaOnnxVoiceActivityDetectorFlush": (None, (_V,)),
    "SherpaOnnxCreateOfflineRecognizer": (_V, (c.POINTER(OfflineRecognizerConfig),)),
    "SherpaOnnxDestroyOfflineRecognizer": (None, (_V,)),
    "SherpaOnnxCreateOfflineStream": (_V, (_V,)),
    "SherpaOnnxDestroyOfflineStream": (None, (_V,)),
    "SherpaOnnxAcceptWaveformOffline": (None, (_V, _I, _FP, _I)),
    "SherpaOnnxDecodeOfflineStream": (None, (_V, _V)),
    "SherpaOnnxGetOfflineStreamResult": (c.POINTER(OfflineRecognizerResult), (_V,)),
    "SherpaOnnxDestroyOfflineRecognizerResult": (None, (c.POINTER(OfflineRecognizerResult),)),
    "SherpaOnnxCreateOfflineTts": (_V, (c.POINTER(OfflineTtsConfig),)),
    "SherpaOnnxDestroyOfflineTts": (None, (_V,)),
    "SherpaOnnxOfflineTtsSampleRate": (_I, (_V,)),
    "SherpaOnnxOfflineTtsGenerateWithConfig": (c.POINTER(GeneratedAudio),
                                               (_V, _P, c.POINTER(GenerationConfig), _V, _V)),
    "SherpaOnnxDestroyOfflineTtsGeneratedAudio": (None, (c.POINTER(GeneratedAudio),)),
    "SherpaOnnxWriteWave": (_I, (_FP, _I, _I, _P)),
}

_loaded = {}
_load_lock = threading.Lock()


def library_path():
    return Path(os.environ.get("BEING_SHERPA_LIBRARY") or LIBRARY)


def load(library=None):
    """Return the bound library. `library` may be a path or an already loaded object.

    Raises Unavailable when the file is missing, will not load, lacks a symbol
    or reports a release whose layout was not checked.
    """
    if library is not None and not isinstance(library, (str, os.PathLike)):
        return _bind(library, getattr(library, "path", "fake"))
    path = Path(library) if library is not None else library_path()
    with _load_lock:
        if str(path) in _loaded:
            return _loaded[str(path)]
        if not path.is_file():
            raise Unavailable(f"the speech library {path} is not installed")
        try:
            handle = c.CDLL(str(path))
        except OSError as error:
            raise Unavailable(f"the speech library would not load ({error})") from error
        lib = _bind(handle, path)
        _loaded[str(path)] = lib
        return lib


def _bind(lib, path):
    for name, (result, args) in SIGNATURES.items():
        try:
            function = getattr(lib, name)
        except AttributeError as error:
            raise Unavailable(f"the speech library at {path} has no {name}") from error
        function.restype = result
        function.argtypes = args
    version = (lib.SherpaOnnxGetVersionStr() or b"").decode(errors="replace").strip()
    if version.lstrip("v") not in CHECKED_VERSIONS:
        raise Unavailable(f"sherpa-onnx {version or 'of unknown version'} has not had its layout checked")
    return lib


def version(library=None):
    lib = load(library)
    sherpa = lib.SherpaOnnxGetVersionStr().decode()
    runtime = (lib.SherpaOnnxGetOnnxruntimeVersionStr() or b"").decode()
    return {"sherpa_onnx": sherpa, "onnxruntime": runtime}


def _path(value):
    return os.fsencode(str(value))


def pcm_to_float(pcm):
    """16-bit little-endian mono PCM bytes to float32 samples in [-1, 1)."""
    samples = array("h")
    samples.frombytes(bytes(pcm))
    if samples.itemsize != 2:  # pragma: no cover - every supported platform has 16-bit shorts
        raise ValueError("16-bit samples are needed")
    return array("f", map(PCM_SCALE.__mul__, samples))


def read_wav(path):
    """Float samples and rate of a mono 16-bit WAV, at whatever rate it was recorded.

    The Moonshine packs' test recordings are 24 kHz. The recognizer resamples
    by itself (pass it the rate); Silero needs 16 kHz, so use `resample`.
    """
    import wave

    with wave.open(str(path)) as audio:
        if (audio.getnchannels(), audio.getsampwidth()) != (1, 2):
            raise ValueError(f"{path} is not mono 16-bit audio")
        rate = audio.getframerate()
        if not 8000 <= rate <= 48000:
            raise ValueError(f"{path} has an unusual sample rate ({rate} Hz)")
        return pcm_to_float(audio.readframes(audio.getnframes())), rate


def resample(samples, source_rate, target_rate=RATE):
    """Linear-interpolation resampling, enough for a voice activity check."""
    if source_rate == target_rate or not samples:
        return samples if isinstance(samples, array) else array("f", samples)
    count = int(len(samples) * target_rate / source_rate)
    step = source_rate / target_rate
    last = len(samples) - 1
    out = array("f", bytes(4 * count))
    for index in range(count):
        position = index * step
        left = int(position)
        right = left + 1 if left < last else last
        fraction = position - left
        out[index] = samples[left] + (samples[right] - samples[left]) * fraction
    return out


def _float_buffer(samples):
    if not isinstance(samples, array) or samples.typecode != "f":
        samples = array("f", samples)
    return samples, (c.c_float * len(samples)).from_buffer(samples) if samples else None


def _floats(pointer, count):
    out = array("f")
    if count > 0 and pointer:
        out.frombytes(c.string_at(pointer, count * c.sizeof(c.c_float)))
    return out


class Vad:
    """Silero VAD: speech start and end over 16 kHz float samples, 512-sample windows."""

    def __init__(self, model, *, threshold=0.5, min_silence=0.3, min_speech=0.25, max_speech=15.0,
                 window=512, rate=RATE, buffer_seconds=60.0, threads=1, library=None):
        self.lib = load(library)
        self.window = window
        self.rate = rate
        config = VadModelConfig()
        config.silero_vad.model = _path(model)
        config.silero_vad.threshold = threshold
        config.silero_vad.min_silence_duration = min_silence
        config.silero_vad.min_speech_duration = min_speech
        config.silero_vad.window_size = window
        config.silero_vad.max_speech_duration = max_speech
        config.sample_rate = rate
        config.num_threads = threads
        config.provider = b"cpu"
        self._config = config
        self.handle = self.lib.SherpaOnnxCreateVoiceActivityDetector(c.pointer(config), buffer_seconds)
        if not self.handle:
            raise Unavailable(f"the voice activity model {model} would not load")

    def accept(self, samples):
        samples, buffer = _float_buffer(samples)
        if buffer is not None:
            self.lib.SherpaOnnxVoiceActivityDetectorAcceptWaveform(self.handle, buffer, len(samples))

    def detected(self):
        return bool(self.lib.SherpaOnnxVoiceActivityDetectorDetected(self.handle))

    def pop_segments(self):
        """Finished speech segments as (start sample, float samples), oldest first."""
        segments = []
        while not self.lib.SherpaOnnxVoiceActivityDetectorEmpty(self.handle):
            front = self.lib.SherpaOnnxVoiceActivityDetectorFront(self.handle)
            if front:
                try:
                    segment = front.contents
                    segments.append((int(segment.start), _floats(segment.samples, segment.n)))
                finally:
                    self.lib.SherpaOnnxDestroySpeechSegment(front)
            self.lib.SherpaOnnxVoiceActivityDetectorPop(self.handle)
        return segments

    def flush(self):
        self.lib.SherpaOnnxVoiceActivityDetectorFlush(self.handle)

    def reset(self):
        self.lib.SherpaOnnxVoiceActivityDetectorReset(self.handle)

    def close(self):
        if self.handle:
            self.lib.SherpaOnnxDestroyVoiceActivityDetector(self.handle)
            self.handle = None


class OfflineRecognizer:
    """Moonshine v2 (encoder plus merged decoder), greedy, on CPU."""

    def __init__(self, encoder, merged_decoder, tokens, *, threads=1, library=None):
        self.lib = load(library)
        config = OfflineRecognizerConfig()
        config.feat_config.sample_rate = RATE
        config.feat_config.feature_dim = 80
        config.model_config.moonshine.encoder = _path(encoder)
        config.model_config.moonshine.merged_decoder = _path(merged_decoder)
        config.model_config.tokens = _path(tokens)
        config.model_config.num_threads = threads
        config.model_config.provider = b"cpu"
        config.decoding_method = b"greedy_search"
        self._config = config
        self.handle = self.lib.SherpaOnnxCreateOfflineRecognizer(c.pointer(config))
        if not self.handle:
            raise Unavailable(f"the Moonshine model in {Path(encoder).parent} would not load")

    def transcribe(self, samples, rate=RATE):
        """Decode one utterance. Blocking; call it from a worker thread."""
        samples, buffer = _float_buffer(samples)
        stream = self.lib.SherpaOnnxCreateOfflineStream(self.handle)
        if not stream:
            raise RuntimeError("could not open a recognition stream")
        try:
            if buffer is not None:
                self.lib.SherpaOnnxAcceptWaveformOffline(stream, rate, buffer, len(samples))
            self.lib.SherpaOnnxDecodeOfflineStream(self.handle, stream)
            result = self.lib.SherpaOnnxGetOfflineStreamResult(stream)
            if not result:
                raise RuntimeError("the recognizer returned no result")
            try:
                return (result.contents.text or b"").decode("utf-8", errors="replace").strip()
            finally:
                self.lib.SherpaOnnxDestroyOfflineRecognizerResult(result)
        finally:
            self.lib.SherpaOnnxDestroyOfflineStream(stream)

    def close(self):
        if self.handle:
            self.lib.SherpaOnnxDestroyOfflineRecognizer(self.handle)
            self.handle = None


class OfflineTts:
    """A resident VITS (Piper) voice."""

    def __init__(self, model, tokens, data_dir, *, threads=1, library=None):
        self.lib = load(library)
        config = OfflineTtsConfig()
        vits = config.model.vits
        vits.model = _path(model)
        vits.tokens = _path(tokens)
        vits.data_dir = _path(data_dir)
        vits.noise_scale = 0.667
        vits.noise_scale_w = 0.8
        vits.length_scale = 1.0
        config.model.num_threads = threads
        config.model.provider = b"cpu"
        config.max_num_sentences = 1
        config.silence_scale = 0.2
        self._config = config
        self.handle = self.lib.SherpaOnnxCreateOfflineTts(c.pointer(config))
        if not self.handle:
            raise Unavailable(f"the voice {Path(model).name} would not load")
        self.sample_rate = int(self.lib.SherpaOnnxOfflineTtsSampleRate(self.handle))

    def _generate(self, text, length_scale):
        if not length_scale or length_scale <= 0:
            raise ValueError("length_scale must be positive")
        options = GenerationConfig()
        options.silence_scale = 0.2
        # VITS turns speed into length_scale = 1 / speed.
        options.speed = 1 / length_scale
        options.sid = 0
        audio = self.lib.SherpaOnnxOfflineTtsGenerateWithConfig(
            self.handle, text.encode("utf-8"), c.pointer(options), None, None)
        if not audio:
            raise RuntimeError("the voice produced no audio")
        return audio

    def generate(self, text, *, length_scale=1.0):
        """Float samples and their rate. Blocking; call it from a worker thread."""
        audio = self._generate(text, length_scale)
        try:
            return _floats(audio.contents.samples, audio.contents.n), int(audio.contents.sample_rate)
        finally:
            self.lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)

    def write(self, text, path, *, length_scale=1.0):
        """Write a 16-bit mono WAV and return its length in seconds."""
        audio = self._generate(text, length_scale)
        try:
            count, rate = int(audio.contents.n), int(audio.contents.sample_rate)
            if count <= 0 or rate <= 0:
                raise RuntimeError("the voice produced no audio")
            if not self.lib.SherpaOnnxWriteWave(audio.contents.samples, count, rate, _path(path)):
                raise RuntimeError(f"could not write {path}")
            return count / rate
        finally:
            self.lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)

    def close(self):
        if self.handle:
            self.lib.SherpaOnnxDestroyOfflineTts(self.handle)
            self.handle = None

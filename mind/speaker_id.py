"""Local CAM++ embeddings for speaker qualification on the target CPU."""

import ctypes as c
import math

from mind import sherpa

RATE = sherpa.RATE
MIN_SECONDS = 1
MAX_SECONDS = 12
_HANDLE = c.c_void_p
_SAMPLES = c.POINTER(c.c_float)
_EMBEDDING_ARGS = (_HANDLE, _HANDLE)
_SIGNATURES = {
    "SherpaOnnxCreateSpeakerEmbeddingExtractor": (
        _HANDLE, (c.POINTER(sherpa.SpeakerEmbeddingExtractorConfig),),
    ),
    "SherpaOnnxDestroySpeakerEmbeddingExtractor": (None, (_HANDLE,)),
    "SherpaOnnxSpeakerEmbeddingExtractorDim": (c.c_int32, (_HANDLE,)),
    "SherpaOnnxSpeakerEmbeddingExtractorCreateStream": (_HANDLE, (_HANDLE,)),
    "SherpaOnnxOnlineStreamAcceptWaveform": (
        None, (_HANDLE, c.c_int32, _SAMPLES, c.c_int32),
    ),
    "SherpaOnnxOnlineStreamInputFinished": (None, (_HANDLE,)),
    "SherpaOnnxSpeakerEmbeddingExtractorIsReady": (c.c_int32, _EMBEDDING_ARGS),
    "SherpaOnnxSpeakerEmbeddingExtractorComputeEmbedding": (_SAMPLES, _EMBEDDING_ARGS),
    "SherpaOnnxSpeakerEmbeddingExtractorDestroyEmbedding": (None, (_SAMPLES,)),
    "SherpaOnnxDestroyOnlineStream": (None, (_HANDLE,)),
}


class InsufficientAudio(ValueError):
    """Sherpa could not extract enough frames from the utterance."""


class Extractor:
    """One CPU thread, one extractor, one short 16 kHz utterance per stream."""

    def __init__(self, model, *, library=None):
        # Bind optional speaker symbols here so a missing one cannot affect Voice.
        self.lib = sherpa.load(library)
        for name, (result, args) in _SIGNATURES.items():
            try:
                function = getattr(self.lib, name)
            except AttributeError as error:
                raise sherpa.Unavailable(f"the speech library has no {name}") from error
            function.restype = result
            function.argtypes = args
        self.config = sherpa.SpeakerEmbeddingExtractorConfig(
            model=sherpa._path(model), num_threads=1, provider=b"cpu",
        )
        self.handle = self.lib.SherpaOnnxCreateSpeakerEmbeddingExtractor(c.byref(self.config))
        if not self.handle:
            raise sherpa.Unavailable("the speaker model could not be opened")
        self.dimension = self.lib.SherpaOnnxSpeakerEmbeddingExtractorDim(self.handle)
        if not 1 <= self.dimension <= 4096:
            self.close()
            raise sherpa.Unavailable("the speaker model returned an invalid embedding dimension")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.handle:
            self.lib.SherpaOnnxDestroySpeakerEmbeddingExtractor(self.handle)
            self.handle = None

    def embedding(self, samples):
        """Return a unit vector for one normalized mono utterance."""
        if not self.handle:
            raise sherpa.Unavailable("the speaker model is closed")
        if not RATE * MIN_SECONDS <= len(samples) <= RATE * MAX_SECONDS:
            raise ValueError("speaker audio must contain one to twelve seconds at 16 kHz")
        samples, buffer = sherpa._float_buffer(samples)
        if not all(-1.0 <= sample <= 1.0 for sample in samples):
            raise ValueError("speaker audio samples must be finite and normalized")
        stream = self.lib.SherpaOnnxSpeakerEmbeddingExtractorCreateStream(self.handle)
        if not stream:
            raise sherpa.Unavailable("the speaker model could not open a stream")
        try:
            self.lib.SherpaOnnxOnlineStreamAcceptWaveform(stream, RATE, buffer, len(samples))
            self.lib.SherpaOnnxOnlineStreamInputFinished(stream)
            if not self.lib.SherpaOnnxSpeakerEmbeddingExtractorIsReady(self.handle, stream):
                raise InsufficientAudio("the speaker model received insufficient audio")
            result = self.lib.SherpaOnnxSpeakerEmbeddingExtractorComputeEmbedding(self.handle, stream)
            if not result:
                raise sherpa.Unavailable("the speaker model returned no embedding")
            try:
                values = tuple(sherpa._floats(result, self.dimension))
            finally:
                self.lib.SherpaOnnxSpeakerEmbeddingExtractorDestroyEmbedding(result)
        finally:
            self.lib.SherpaOnnxDestroyOnlineStream(stream)

        norm = math.hypot(*values)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError("the speaker model returned an unusable embedding")
        return tuple(value / norm for value in values)

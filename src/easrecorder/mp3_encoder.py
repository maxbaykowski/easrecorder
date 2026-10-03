"""MP3 encoding through the native LAME library (libmp3lame), loaded with ctypes.

Ported from NWR Stream Manager (src/nwr-stream-manager/encoder.py, Mp3Encoder), where
it replaced the lameenc Python package and produced byte-identical output. The calls
mirror what lameenc does, so recordings encode the same as before.
"""

from __future__ import annotations

import ctypes
import ctypes.util

import numpy as np

LAME_MONO = 3  # MPEG_mode MONO
LAME_FLUSH_BUFFER_BYTES = 8 * 1024


class Mp3EncoderError(RuntimeError):
    """Raised when libmp3lame can't be loaded or rejects a setting."""


class Mp3Encoder:
    """Constant-bitrate mono MP3 from 16-bit PCM at one of MPEG's sample rates."""

    def __init__(self, sample_rate: int, bitrate: int, quality: int = 2) -> None:
        self.gfp = None
        self.lame = _load_shared_library("mp3lame", ("libmp3lame.so.0", "libmp3lame.so"))
        _configure_ctypes(self.lame)
        self.gfp = self.lame.lame_init()
        if not self.gfp:
            raise Mp3EncoderError("LAME could not create an MP3 encoder")
        try:
            for setter, value in (
                (self.lame.lame_set_num_channels, 1),
                (self.lame.lame_set_in_samplerate, int(sample_rate)),
                (self.lame.lame_set_out_samplerate, int(sample_rate)),
                (self.lame.lame_set_brate, int(bitrate)),
                (self.lame.lame_set_quality, int(quality)),
                # lameenc writes no Xing/VBR info frame; match it.
                (self.lame.lame_set_bWriteVbrTag, 0),
                (self.lame.lame_set_mode, LAME_MONO),
            ):
                if setter(self.gfp, value) < 0:
                    raise Mp3EncoderError(f"LAME rejected {setter.__name__}({value})")
            if self.lame.lame_init_params(self.gfp) < 0:
                raise Mp3EncoderError(f"LAME could not start a {bitrate} kbps MP3 encoder at {sample_rate} Hz")
        except Exception:
            self.close()
            raise

    def encode(self, pcm: bytes) -> bytes:
        """Encode signed 16-bit little-endian mono PCM; returns whatever MP3 data is ready."""

        if not self.gfp:
            raise Mp3EncoderError("the MP3 encoder is closed")
        samples = np.ascontiguousarray(np.frombuffer(pcm, dtype="<i2"), dtype=np.int16)
        if samples.size == 0:
            return b""
        # LAME's own worst case for one call: 1.25 bytes per sample plus 7200.
        output = ctypes.create_string_buffer(samples.size + samples.size // 4 + 7200)
        written = self.lame.lame_encode_buffer(
            self.gfp, samples.ctypes.data, samples.ctypes.data, samples.size, output, len(output)
        )
        if written < 0:
            raise Mp3EncoderError(f"LAME MP3 encoding failed with error {written}")
        return output.raw[:written]

    def flush(self) -> bytes:
        """Return the last MP3 frames. The encoder can't be used after this."""

        if not self.gfp:
            return b""
        output = ctypes.create_string_buffer(LAME_FLUSH_BUFFER_BYTES)
        written = self.lame.lame_encode_flush(self.gfp, output, len(output))
        if written < 0:
            raise Mp3EncoderError(f"LAME MP3 flush failed with error {written}")
        return output.raw[:written]

    def close(self) -> None:
        gfp = self.gfp
        if gfp:
            self.gfp = None
            self.lame.lame_close(gfp)

    def __enter__(self) -> Mp3Encoder:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def _configure_ctypes(lame: ctypes.CDLL) -> None:
    pointer = ctypes.c_void_p
    lame.lame_init.restype = pointer
    lame.lame_init.argtypes = []
    for name in (
        "lame_set_num_channels",
        "lame_set_in_samplerate",
        "lame_set_out_samplerate",
        "lame_set_brate",
        "lame_set_quality",
        "lame_set_bWriteVbrTag",
        "lame_set_mode",
    ):
        function = getattr(lame, name)
        function.restype = ctypes.c_int
        function.argtypes = [pointer, ctypes.c_int]
    lame.lame_init_params.restype = ctypes.c_int
    lame.lame_init_params.argtypes = [pointer]
    lame.lame_encode_buffer.restype = ctypes.c_int
    lame.lame_encode_buffer.argtypes = [pointer, pointer, pointer, ctypes.c_int, pointer, ctypes.c_int]
    lame.lame_encode_flush.restype = ctypes.c_int
    lame.lame_encode_flush.argtypes = [pointer, pointer, ctypes.c_int]
    lame.lame_close.restype = ctypes.c_int
    lame.lame_close.argtypes = [pointer]


def _load_shared_library(name: str, sonames: tuple[str, ...]) -> ctypes.CDLL:
    candidates = [ctypes.util.find_library(name), *sonames]
    errors = []
    for candidate in dict.fromkeys(filter(None, candidates)):
        try:
            return ctypes.CDLL(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    detail = "; ".join(errors) if errors else f"ctypes could not locate {name}"
    raise Mp3EncoderError(f"required shared library '{name}' could not be loaded: {detail}")

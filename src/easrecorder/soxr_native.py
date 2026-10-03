"""Sample-rate conversion through the native libsoxr library, loaded with ctypes.

Ported from NWR Stream Manager (src/nwr-stream-manager/soxr_native.py), where it
replaced the soxr Python package. It drives libsoxr the same way that package does
(high quality, whole chunks in, output as early as libsoxr allows), so results match
it, without depending on a package that is only on PyPI.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading

import numpy as np
from numpy.typing import NDArray

SOXR_SONAMES = ("libsoxr.so.0", "libsoxr.so")
SOXR_FLOAT32_I = 0
SOXR_INT16_I = 3
SOXR_HQ = 4  # 20-bit precision, the soxr Python package's default "HQ"
SOXR_NO_DITHER = 8
# Long chunks are fed in pieces of about this many output samples, as the Python package does.
SOXR_CHUNK_OUTPUT_SAMPLES = 48_000

_DATATYPES = {"float32": (SOXR_FLOAT32_I, np.float32), "int16": (SOXR_INT16_I, np.int16)}


class _IoSpec(ctypes.Structure):
    _fields_ = [
        ("itype", ctypes.c_int),
        ("otype", ctypes.c_int),
        ("scale", ctypes.c_double),
        ("e", ctypes.c_void_p),
        ("flags", ctypes.c_ulong),
    ]


class _QualitySpec(ctypes.Structure):
    _fields_ = [
        ("precision", ctypes.c_double),
        ("phase_response", ctypes.c_double),
        ("passband_end", ctypes.c_double),
        ("stopband_begin", ctypes.c_double),
        ("e", ctypes.c_void_p),
        ("flags", ctypes.c_ulong),
    ]


_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()


def soxr_library() -> ctypes.CDLL:
    global _library
    with _library_lock:
        if _library is not None:
            return _library
        names = [name for name in (ctypes.util.find_library("soxr"), *SOXR_SONAMES) if name]
        errors = []
        for name in names:
            try:
                library = ctypes.CDLL(name)
                break
            except OSError as exc:
                errors.append(str(exc))
        else:
            raise OSError("libsoxr is not installed: " + "; ".join(errors))
        pointer = ctypes.c_void_p
        size_pointer = ctypes.POINTER(ctypes.c_size_t)
        library.soxr_io_spec.restype = _IoSpec
        library.soxr_io_spec.argtypes = [ctypes.c_int, ctypes.c_int]
        library.soxr_quality_spec.restype = _QualitySpec
        library.soxr_quality_spec.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        library.soxr_create.restype = pointer
        library.soxr_create.argtypes = [
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(_IoSpec),
            ctypes.POINTER(_QualitySpec),
            pointer,
        ]
        library.soxr_process.restype = ctypes.c_char_p
        library.soxr_process.argtypes = [pointer, pointer, ctypes.c_size_t, size_pointer, pointer, ctypes.c_size_t, size_pointer]
        library.soxr_delay.restype = ctypes.c_double
        library.soxr_delay.argtypes = [pointer]
        library.soxr_delete.restype = None
        library.soxr_delete.argtypes = [pointer]
        _library = library
        return library


class SoxrStream:
    """Resample one channel of audio in a stream of chunks of any length."""

    def __init__(self, input_rate: float, output_rate: float, dtype: str = "float32") -> None:
        if dtype not in _DATATYPES:
            raise ValueError(f"unsupported soxr sample type: {dtype}")
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("sample rates must be greater than 0")
        self.input_rate = float(input_rate)
        self.output_rate = float(output_rate)
        self._datatype, self.dtype = _DATATYPES[dtype]
        self._ratio = self.output_rate / self.input_rate
        self._chunk_input = max(1000, int(SOXR_CHUNK_OUTPUT_SAMPLES / self._ratio))
        self._library = soxr_library()
        io_spec = self._library.soxr_io_spec(self._datatype, self._datatype)
        # libsoxr dithers 16-bit output with randomness seeded from the clock, so the same
        # audio came out slightly different every time. Without it, output is repeatable
        # and matches the soxr Python package this replaced.
        io_spec.flags |= SOXR_NO_DITHER
        quality_spec = self._library.soxr_quality_spec(SOXR_HQ, 0)
        error = ctypes.c_char_p()
        self._handle = self._library.soxr_create(
            self.input_rate,
            self.output_rate,
            1,
            ctypes.byref(error),
            ctypes.byref(io_spec),
            ctypes.byref(quality_spec),
            None,
        )
        if error.value or not self._handle:
            raise RuntimeError(f"libsoxr could not create a resampler: {(error.value or b'unknown error').decode()}")
        self._ended = False
        self._done_value = ctypes.c_size_t(0)
        self._done = ctypes.byref(self._done_value)

    def process(self, samples: NDArray, *, last: bool = False) -> NDArray:
        if self._ended:
            raise RuntimeError("input after the last chunk")
        samples = np.ascontiguousarray(samples, dtype=self.dtype)
        item = samples.itemsize
        process = self._library.soxr_process
        handle, done = self._handle, self._done
        # Room for this chunk's output plus whatever libsoxr is still holding back, sized
        # like the soxr Python package so output arrives in the same pieces; the loops
        # below grow it in the rare case that is not enough.
        capacity = int(self._library.soxr_delay(handle) + samples.size * self._ratio) + 1
        output = np.empty(max(capacity, 1024), dtype=self.dtype)
        produced = 0
        for start in range(0, samples.size, self._chunk_input):
            count = min(self._chunk_input, samples.size - start)
            if produced >= output.size:
                output = np.concatenate((output, np.empty(output.size, dtype=self.dtype)))
            self._check(
                process(
                    handle,
                    samples.ctypes.data + start * item,
                    count,
                    None,  # libsoxr takes the whole piece and buffers what it cannot output yet
                    output.ctypes.data + produced * item,
                    output.size - produced,
                    done,
                )
            )
            produced += self._done_value.value
        if last:
            self._ended = True
            while True:
                if produced >= output.size:
                    output = np.concatenate((output, np.empty(output.size, dtype=self.dtype)))
                self._check(process(handle, None, 0, None, output.ctypes.data + produced * item, output.size - produced, done))
                if self._done_value.value == 0:
                    break
                produced += self._done_value.value
        return output[:produced]

    @staticmethod
    def _check(error: bytes | None) -> None:
        if error:
            raise RuntimeError(f"libsoxr failed: {error.decode()}")

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.soxr_delete(handle)
            except Exception:
                pass
            self._handle = None

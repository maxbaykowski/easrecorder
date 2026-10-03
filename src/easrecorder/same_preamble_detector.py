"""SAME (Specific Area Message Encoding) preamble detection for 16-bit mono PCM.

Extracted from NWR Stream Manager (last present there in commit 3bab720, in
src/nwr-stream-manager/same_live.py), where it found SAME bursts in live audio so they
could be muted before streaming. It works on any sample rate and needs only numpy.

Two checks, cheapest first:

* SameToneDetector.is_same_like(frame) asks whether a short frame (about 20 ms) is
  dominated by the SAME mark and space tones. It is fast enough to run on every frame.
* SameToneDetector.has_same_preamble(pcm) looks for the 0xAB preamble bit pattern
  (at least SAME_PREAMBLE_DETECT_MIN_BITS bits in a row) in the last
  SAME_PREAMBLE_DETECT_SECONDS of audio. Run it once enough frames in a row look like
  SAME, as SamePreambleTracker does for a stream and find_same_preambles does for a
  whole recording.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAME_MARK_HZ = 2083.3
SAME_SPACE_HZ = 1562.5
SAME_BAUD = 520.83
SAME_PREAMBLE_BYTE = 0xAB
# How long the tones must last before the (slower) preamble check runs.
SAME_DETECT_MIN_SECONDS = 0.08
# How long the tones must be gone before a burst is over and the next can be found.
SAME_DETECT_END_SECONDS = 0.18
SAME_PREAMBLE_DETECT_MIN_BITS = 28
SAME_PREAMBLE_DETECT_SECONDS = 0.36
FRAME_SECONDS = 0.02
LOOKBEHIND_SECONDS = 0.22


class SameToneDetector:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self._cache: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self._symbol_cache: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self._preamble_bits = tuple((SAME_PREAMBLE_BYTE >> bit_index) & 1 for bit_index in range(8))

    def is_same_like(self, pcm_s16le: bytes) -> bool:
        if not pcm_s16le:
            return False
        samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32)
        if samples.size < 32:
            return False
        samples -= float(np.mean(samples))
        total = float(np.dot(samples, samples))
        if total <= 1.0:
            return False
        mark_i, mark_q, space_i, space_q = self._basis(samples.size)
        mark = float(np.dot(samples, mark_i) ** 2 + np.dot(samples, mark_q) ** 2)
        space = float(np.dot(samples, space_i) ** 2 + np.dot(samples, space_q) ** 2)
        # A pure tone's ratio is samples.size / 2, so scale by that to keep frames of
        # any sample rate comparable. The fractions equal the original thresholds
        # (24, 4 and 48) at 24 kHz, the rate they were tuned at.
        full_scale = samples.size / 2.0
        dominant_ratio = max(mark, space) / total / full_scale
        weaker_ratio = min(mark, space) / total / full_scale
        combined_ratio = (mark + space) / total / full_scale
        if dominant_ratio < 0.10 or weaker_ratio < 1.0 / 60.0 or combined_ratio < 0.20:
            return False
        return True

    def has_same_preamble(self, pcm_s16le: bytes) -> bool:
        if not pcm_s16le:
            return False
        samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32)
        if samples.size < round(self.sample_rate * 0.12):
            return False
        max_samples = round(self.sample_rate * SAME_PREAMBLE_DETECT_SECONDS)
        if samples.size > max_samples:
            samples = samples[-max_samples:]
        samples -= float(np.mean(samples))
        total = float(np.dot(samples, samples))
        if total <= 1.0:
            return False
        mark_i, mark_q, space_i, space_q = self._basis(samples.size)
        mark = float(np.dot(samples, mark_i) ** 2 + np.dot(samples, mark_q) ** 2)
        space = float(np.dot(samples, space_i) ** 2 + np.dot(samples, space_q) ** 2)
        if (mark + space) / total < 0.10:
            return False
        return self._has_same_preamble_pattern(samples)

    def _has_same_preamble_pattern(self, samples: np.ndarray) -> bool:
        symbol_samples = max(8, round(self.sample_rate / SAME_BAUD))
        if samples.size < symbol_samples * SAME_PREAMBLE_DETECT_MIN_BITS:
            return False
        best_run = 0
        offset_step = max(1, symbol_samples // 6)
        for start_offset in range(0, symbol_samples, offset_step):
            symbols: list[int] = []
            confidence: list[bool] = []
            for start in range(start_offset, samples.size - symbol_samples + 1, symbol_samples):
                symbol = samples[start : start + symbol_samples]
                total = float(np.dot(symbol, symbol))
                if total <= 1.0:
                    symbols.append(0)
                    confidence.append(False)
                    continue
                mark_i, mark_q, space_i, space_q = self._symbol_basis(symbol.size)
                mark = float(np.dot(symbol, mark_i) ** 2 + np.dot(symbol, mark_q) ** 2)
                space = float(np.dot(symbol, space_i) ** 2 + np.dot(symbol, space_q) ** 2)
                symbols.append(1 if mark > space else 0)
                confidence.append(
                    max(mark, space) / total >= 0.14
                    and abs(mark - space) / max(mark, space) >= 0.10
                )
            if len(symbols) < SAME_PREAMBLE_DETECT_MIN_BITS:
                continue
            for phase in range(8):
                run = 0
                for index, bit in enumerate(symbols):
                    expected = self._preamble_bits[(index + phase) % 8]
                    if confidence[index] and bit == expected:
                        run += 1
                        best_run = max(best_run, run)
                    else:
                        run = 0
        return best_run >= SAME_PREAMBLE_DETECT_MIN_BITS

    def _symbol_basis(self, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        cached = self._symbol_cache.get(size)
        if cached is not None:
            return cached
        t = np.arange(size, dtype=np.float32) / float(self.sample_rate)
        basis = (
            np.sin(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.sin(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
        )
        self._symbol_cache[size] = basis
        return basis

    def _basis(self, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        cached = self._cache.get(size)
        if cached is not None:
            return cached
        t = np.arange(size, dtype=np.float32) / float(self.sample_rate)
        basis = (
            np.sin(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.sin(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
        )
        self._cache[size] = basis
        return basis


@dataclass
class SameBurst:
    """One SAME burst, in samples counted from the first sample fed to the tracker.

    end stays None until the tones have stopped long enough for the burst to be over.
    """

    start: int
    end: int | None = None


class SamePreambleTracker:
    """Finds each SAME burst in 16-bit mono PCM fed in chunks of any size.

    feed() returns a SameBurst for each burst whose preamble was just confirmed, with
    start set to the sample where its SAME tones began. The tracker fills in end later,
    once the tones stop, so callers can tell short EOM bursts from headers. It scans in
    20 ms frames the way NWR Stream Manager did live: once enough frames in a row look
    like SAME tones, the last ~0.22 s is checked for the preamble pattern. Each burst is
    reported once; the next is looked for after the tones have stopped.
    """

    def __init__(self, sample_rate: int) -> None:
        self.detector = SameToneDetector(sample_rate)
        self.frame_samples = max(1, round(sample_rate * FRAME_SECONDS))
        self._lookbehind_frames = round(LOOKBEHIND_SECONDS / FRAME_SECONDS)
        self._min_tone_frames = round(SAME_DETECT_MIN_SECONDS / FRAME_SECONDS)
        self._end_quiet_frames = round(SAME_DETECT_END_SECONDS / FRAME_SECONDS)
        self._remainder = b""
        self._frame_index = 0
        self._pending: list[bytes] = []
        self._tone_frames = 0
        self._quiet_frames = 0
        self._in_burst = False
        self._current: SameBurst | None = None

    def feed(self, pcm_s16le: bytes) -> list[SameBurst]:
        data = self._remainder + pcm_s16le
        frame_bytes = self.frame_samples * 2
        usable = len(data) - (len(data) % frame_bytes)
        self._remainder = data[usable:]
        found: list[SameBurst] = []
        for offset in range(0, usable, frame_bytes):
            start = self._feed_frame(data[offset : offset + frame_bytes])
            if start is not None:
                found.append(start)
        return found

    def _feed_frame(self, pcm: bytes) -> SameBurst | None:
        frame_index = self._frame_index
        self._frame_index += 1
        same_like = self.detector.is_same_like(pcm)
        if self._in_burst:
            self._quiet_frames = 0 if same_like else self._quiet_frames + 1
            if self._quiet_frames >= self._end_quiet_frames:
                self._in_burst = False
                if self._current is not None:
                    self._current.end = (frame_index - self._quiet_frames + 1) * self.frame_samples
                    self._current = None
            return None
        self._pending.append(pcm)
        if len(self._pending) > self._lookbehind_frames:
            self._pending.pop(0)
        self._tone_frames = self._tone_frames + 1 if same_like else 0
        if self._tone_frames >= self._min_tone_frames and self.detector.has_same_preamble(b"".join(self._pending)):
            self._current = SameBurst((frame_index - self._tone_frames + 1) * self.frame_samples)
            self._in_burst, self._quiet_frames, self._tone_frames = True, 0, 0
            self._pending.clear()
            return self._current
        return None


def find_same_preambles(samples: np.ndarray, sample_rate: int) -> list[float]:
    """Times, in seconds, where each SAME burst begins in mono float audio (-1 to 1)."""
    pcm = np.clip(np.asarray(samples, dtype=np.float64) * 32767.0, -32768, 32767).astype("<i2").tobytes()
    return [burst.start / sample_rate for burst in SamePreambleTracker(sample_rate).feed(pcm)]

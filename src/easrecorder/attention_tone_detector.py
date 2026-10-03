"""Attention tone detection for 16-bit mono PCM, like an EAS decoder (ENDEC) does.

Two attention tones follow SAME headers:

* NOAA Weather Radio's single 1050 Hz tone ("nwr").
* The two-tone EBS/EAS signal, 853 Hz and 960 Hz together ("ebs").

Each 0.1 s frame is checked for how much of its 200-4000 Hz energy (the part of the
spectrum broadcast audio carries) sits in a narrow band around those frequencies. A
tone needs most of that energy in its band(s) and little in the other tone's, which
rules out voice, music, SAME bursts and background noise, since they spread their
energy much more widely. The band is wide enough to catch
transmitters a little off frequency. A tone is only reported once it has lasted
ATTENTION_TONE_MIN_SECONDS, and short dropouts (noise, fading) don't end it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NWR_TONE_HZ = 1050.0
EBS_TONE_HZ = (853.0, 960.0)
# How far from the nominal frequency a tone may be and still count.
TONE_BAND_HZ = 30.0
FRAME_SECONDS = 0.1
ATTENTION_TONE_MIN_SECONDS = 2.0
# Gaps in a tone shorter than this (noise, fading) don't end it.
ATTENTION_TONE_MAX_GAP_SECONDS = 0.3
# Frames quieter than this (about -50 dBFS) are never a tone.
MIN_FRAME_RMS = 100.0
# Energy is compared within this range; hiss above it shouldn't hide a tone.
COMPARE_BAND_HZ = (200.0, 4000.0)


@dataclass
class AttentionTone:
    """One attention tone, in samples counted from the first sample fed to the tracker.

    end stays None while the tone is still playing.
    """

    kind: str
    start: int
    end: int | None = None


class AttentionToneDetector:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self.frame_samples = max(16, round(self.sample_rate * FRAME_SECONDS))
        self._window = np.hanning(self.frame_samples).astype(np.float32)
        freqs = np.fft.rfftfreq(self.frame_samples, 1.0 / self.sample_rate)
        self._nwr_band = np.abs(freqs - NWR_TONE_HZ) <= TONE_BAND_HZ
        self._ebs_low_band = np.abs(freqs - EBS_TONE_HZ[0]) <= TONE_BAND_HZ
        self._ebs_high_band = np.abs(freqs - EBS_TONE_HZ[1]) <= TONE_BAND_HZ
        self._compare_band = (freqs >= COMPARE_BAND_HZ[0]) & (freqs <= COMPARE_BAND_HZ[1])

    def classify(self, pcm_s16le: bytes) -> str | None:
        """Return "nwr", "ebs" or None for one frame of frame_samples samples."""

        samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32)
        if samples.size != self.frame_samples:
            return None
        samples -= float(np.mean(samples))
        if float(np.sqrt(np.mean(samples * samples))) < MIN_FRAME_RMS:
            return None
        power = np.abs(np.fft.rfft(samples * self._window)) ** 2
        total = float(np.sum(power[self._compare_band]))
        if total <= 0.0:
            return None
        nwr = float(np.sum(power[self._nwr_band])) / total
        ebs_low = float(np.sum(power[self._ebs_low_band])) / total
        ebs_high = float(np.sum(power[self._ebs_high_band])) / total
        if nwr >= 0.3 and ebs_low < 0.1 and ebs_high < 0.1:
            return "nwr"
        # The two EBS tones are often unequal after audio processing, so the weaker
        # only has to be clearly present.
        if min(ebs_low, ebs_high) >= 0.05 and ebs_low + ebs_high >= 0.3 and nwr < 0.1:
            return "ebs"
        return None


class AttentionToneTracker:
    """Finds attention tones in PCM fed in chunks of any size.

    feed() returns an AttentionTone once a tone has lasted ATTENTION_TONE_MIN_SECONDS;
    the tracker fills in its end once the tone stops.
    """

    def __init__(self, sample_rate: int) -> None:
        self.detector = AttentionToneDetector(sample_rate)
        self.frame_samples = self.detector.frame_samples
        self._min_frames = round(ATTENTION_TONE_MIN_SECONDS / FRAME_SECONDS)
        self._max_gap_frames = round(ATTENTION_TONE_MAX_GAP_SECONDS / FRAME_SECONDS)
        self._remainder = b""
        self._frame_index = 0
        self._kind: str | None = None
        self._run_start = 0
        self._last_tone_frame = 0
        self._current: AttentionTone | None = None

    def feed(self, pcm_s16le: bytes) -> list[AttentionTone]:
        data = self._remainder + pcm_s16le
        frame_bytes = self.frame_samples * 2
        usable = len(data) - (len(data) % frame_bytes)
        self._remainder = data[usable:]
        found: list[AttentionTone] = []
        for offset in range(0, usable, frame_bytes):
            tone = self._feed_frame(data[offset : offset + frame_bytes])
            if tone is not None:
                found.append(tone)
        return found

    def _feed_frame(self, pcm: bytes) -> AttentionTone | None:
        frame_index = self._frame_index
        self._frame_index += 1
        kind = self.detector.classify(pcm)

        if self._kind is not None and kind != self._kind:
            if frame_index - self._last_tone_frame <= self._max_gap_frames and kind is None:
                return None
            self._end_run()
        if kind is None:
            return None
        if self._kind is None:
            self._kind = kind
            self._run_start = frame_index
        self._last_tone_frame = frame_index
        if self._current is None and frame_index - self._run_start + 1 >= self._min_frames:
            self._current = AttentionTone(kind, self._run_start * self.frame_samples)
            return self._current
        return None

    def _end_run(self) -> None:
        if self._current is not None:
            self._current.end = (self._last_tone_frame + 1) * self.frame_samples
        self._kind = None
        self._current = None


def find_attention_tones(samples: np.ndarray, sample_rate: int) -> list[tuple[str, float, float | None]]:
    """(kind, start, end) in seconds for each attention tone in mono float audio (-1 to 1)."""

    pcm = np.clip(np.asarray(samples, dtype=np.float64) * 32767.0, -32768, 32767).astype("<i2").tobytes()
    tones = AttentionToneTracker(sample_rate).feed(pcm)
    return [
        (tone.kind, tone.start / sample_rate, None if tone.end is None else tone.end / sample_rate)
        for tone in tones
    ]

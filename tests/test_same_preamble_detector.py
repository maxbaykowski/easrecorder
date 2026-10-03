import math
import unittest

import numpy as np

from easrecorder import same_preamble_detector as spd


def same_burst(payload: str, sample_rate: int, amplitude: float = 0.73) -> np.ndarray:
    """Phase-continuous SAME AFSK: 16 x 0xAB preamble, 7-bit ASCII LSB first, 3 NULs."""
    data = [spd.SAME_PREAMBLE_BYTE] * 16 + [ord(ch) & 0x7F for ch in payload] + [0] * 3
    phase, cursor, target, output = 0.0, 0, 0.0, []
    for byte in data:
        for bit_index in range(8):
            target += sample_rate / spd.SAME_BAUD
            count = int(round(target)) - cursor
            cursor += count
            step = 2 * math.pi * (spd.SAME_MARK_HZ if (byte >> bit_index) & 1 else spd.SAME_SPACE_HZ) / sample_rate
            for _ in range(count):
                output.append(amplitude * math.sin(phase))
                phase = (phase + step) % (2 * math.pi)
    return np.asarray(output, dtype=np.float32)


def to_pcm(samples: np.ndarray) -> bytes:
    return np.clip(samples * 32767.0, -32768, 32767).astype("<i2").tobytes()


class SamePreambleDetectorTests(unittest.TestCase):
    def test_identifies_a_same_preamble(self) -> None:
        sample_rate = 24_000
        detector = spd.SameToneDetector(sample_rate)
        burst = same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
        self.assertTrue(detector.has_same_preamble(to_pcm(burst[: round(sample_rate * 0.36)])))

    def test_rejects_voice_and_the_attention_tone(self) -> None:
        sample_rate = 24_000
        detector = spd.SameToneDetector(sample_rate)
        t = np.arange(round(sample_rate * 0.4), dtype=np.float32) / sample_rate
        voice_like = 0.25 * np.sin(2 * np.pi * 420 * t) + 0.12 * np.sin(2 * np.pi * 930 * t) + 0.06 * np.sin(2 * np.pi * 1850 * t)
        attention_tone = 0.35 * np.sin(2 * np.pi * 1050 * t)
        self.assertFalse(detector.has_same_preamble(to_pcm(voice_like)))
        self.assertFalse(detector.has_same_preamble(to_pcm(attention_tone)))

    def test_finds_each_burst_in_a_recording_and_nothing_in_speech(self) -> None:
        sample_rate = 22_050
        t = np.arange(sample_rate * 3) / sample_rate
        speech = (0.2 * np.sin(2 * np.pi * 300 * t) * np.sin(2 * np.pi * 3 * t)).astype(np.float32)
        burst = same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
        gap = np.zeros(sample_rate, dtype=np.float32)
        recording = np.concatenate((speech, burst, gap, burst, gap, burst, speech))

        found = spd.find_same_preambles(recording, sample_rate)

        burst_starts = [3.0, 3.0 + (burst.size / sample_rate) + 1.0, 3.0 + 2 * ((burst.size / sample_rate) + 1.0)]
        self.assertEqual(len(found), 3, found)
        for start, time in zip(burst_starts, found):
            self.assertLess(abs(time - start), spd.FRAME_SECONDS + 0.005)

    def test_finds_only_the_bursts_in_a_noisy_alert(self) -> None:
        sample_rate = 22_050
        rng = np.random.default_rng(1)
        t = np.arange(sample_rate * 8) / sample_rate
        # Voice-like audio: a wandering pitch with harmonics and a syllable rhythm.
        pitch = 160 + 40 * np.sin(2 * np.pi * 0.7 * t)
        phase = 2 * np.pi * np.cumsum(pitch) / sample_rate
        voice = sum(np.sin(k * phase) / k for k in range(1, 12)) * 0.3 * np.abs(np.sin(2 * np.pi * 2.5 * t))
        voice = voice.astype(np.float32)
        tone_t = np.arange(sample_rate * 8) / sample_rate
        attention_tone = (0.5 * np.sin(2 * np.pi * 1050 * tone_t)).astype(np.float32)
        header = same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
        eom = same_burst("NNNN", sample_rate)
        gap = np.zeros(sample_rate, dtype=np.float32)

        pieces = [voice, header, gap, header, gap, header, gap, attention_tone, gap, voice, gap, eom, gap, eom, gap, eom, voice]
        recording = np.concatenate(pieces)
        recording += rng.normal(0.0, 0.02, recording.size).astype(np.float32)

        burst_starts = []
        position = 0
        for piece in pieces:
            if piece is header or piece is eom:
                burst_starts.append(position / sample_rate)
            position += piece.size

        found = spd.find_same_preambles(recording, sample_rate)

        self.assertEqual(len(found), len(burst_starts), found)
        for start, time in zip(burst_starts, found):
            self.assertLess(abs(time - start), spd.FRAME_SECONDS + 0.005)

    def test_finds_bursts_at_common_stream_rates(self) -> None:
        for sample_rate in (8_000, 11_025, 16_000, 44_100, 48_000):
            with self.subTest(sample_rate=sample_rate):
                burst = same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
                gap = np.zeros(sample_rate, dtype=np.float32)
                found = spd.find_same_preambles(np.concatenate((gap, burst, gap)), sample_rate)
                self.assertEqual(len(found), 1, found)
                self.assertLess(abs(found[0] - 1.0), spd.FRAME_SECONDS + 0.005)

    def test_tracker_finds_burst_starts_in_odd_sized_chunks(self) -> None:
        sample_rate = 22_050
        burst = same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
        gap = np.zeros(sample_rate, dtype=np.float32)
        pcm = to_pcm(np.concatenate((gap, burst, gap, burst)))
        tracker = spd.SamePreambleTracker(sample_rate)

        found = []
        for offset in range(0, len(pcm), 1555):
            found.extend(tracker.feed(pcm[offset : offset + 1555]))

        burst_starts = [sample_rate, 2 * sample_rate + burst.size]
        self.assertEqual(len(found), 2, found)
        for start, found_burst in zip(burst_starts, found):
            self.assertLess(abs(found_burst.start - start), tracker.frame_samples + 1)

    def test_tracker_reports_burst_length_once_tones_stop(self) -> None:
        sample_rate = 22_050
        header = same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
        eom = same_burst("NNNN", sample_rate)
        gap = np.zeros(sample_rate, dtype=np.float32)
        tracker = spd.SamePreambleTracker(sample_rate)

        found = tracker.feed(to_pcm(np.concatenate((gap, header))))
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].end)
        found += tracker.feed(to_pcm(np.concatenate((gap, eom, gap))))

        lengths = [(burst.end - burst.start) / sample_rate for burst in found]
        self.assertLess(abs(lengths[0] - header.size / sample_rate), 0.1)
        self.assertLess(abs(lengths[1] - eom.size / sample_rate), 0.1)


if __name__ == "__main__":
    unittest.main()

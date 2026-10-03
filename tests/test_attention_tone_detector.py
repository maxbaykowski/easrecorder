import unittest

import numpy as np

from easrecorder import attention_tone_detector as atd
from test_same_preamble_detector import same_burst


def sine(sample_rate, seconds, *freqs, amplitude=0.3):
    t = np.arange(round(sample_rate * seconds)) / sample_rate
    return sum(amplitude * np.sin(2 * np.pi * freq * t) for freq in freqs) / len(freqs)


def with_noise(signal, snr_db, seed=0):
    """Add white noise across the whole spectrum at the given signal-to-noise ratio."""
    power = float(np.mean(signal * signal))
    rng = np.random.default_rng(seed)
    return signal + rng.normal(0.0, np.sqrt(power / 10 ** (snr_db / 10)), signal.size)


def voice_like(sample_rate, seconds):
    t = np.arange(round(sample_rate * seconds)) / sample_rate
    pitch = 150 + 30 * np.sin(2 * np.pi * 0.5 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / sample_rate
    return sum(np.sin(k * phase) / k for k in range(1, 15)) * 0.2 * np.abs(np.sin(2 * np.pi * 2 * t))


def silence(sample_rate, seconds):
    return np.zeros(round(sample_rate * seconds))


class AttentionToneDetectorTests(unittest.TestCase):
    def assert_one_tone(self, recording, sample_rate, kind, start, end):
        found = atd.find_attention_tones(recording, sample_rate)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], kind)
        self.assertLess(abs(found[0][1] - start), atd.FRAME_SECONDS + 0.01)
        self.assertLess(abs(found[0][2] - end), atd.FRAME_SECONDS + 0.01)

    def test_finds_nwr_tone_between_voice(self):
        sr = 22_050
        recording = np.concatenate((voice_like(sr, 3), sine(sr, 9, 1050), voice_like(sr, 3)))
        self.assert_one_tone(recording, sr, "nwr", 3.0, 12.0)

    def test_finds_ebs_tone_between_voice(self):
        sr = 22_050
        recording = np.concatenate((voice_like(sr, 3), sine(sr, 9, 853, 960), voice_like(sr, 3)))
        self.assert_one_tone(recording, sr, "ebs", 3.0, 12.0)

    def test_finds_tones_under_noise_stronger_than_the_tone(self):
        for sr in (8_000, 22_050, 48_000):
            for freqs, kind in (((1050,), "nwr"), ((853, 960), "ebs")):
                with self.subTest(sample_rate=sr, kind=kind):
                    recording = np.concatenate((silence(sr, 2), sine(sr, 9, *freqs), silence(sr, 2)))
                    self.assert_one_tone(with_noise(recording, -2), sr, kind, 2.0, 11.0)

    def test_finds_slightly_off_frequency_and_unbalanced_tones(self):
        sr = 22_050
        cases = (
            (sine(sr, 9, 1030), "nwr"),
            (sine(sr, 9, 1070), "nwr"),
            (sine(sr, 9, 840, 975), "ebs"),
            (sine(sr, 9, 853) * 1.5 + sine(sr, 9, 960) * 0.5, "ebs"),
        )
        for tone, kind in cases:
            with self.subTest(kind=kind):
                recording = np.concatenate((silence(sr, 1), tone, silence(sr, 1)))
                self.assert_one_tone(recording, sr, kind, 1.0, 10.0)

    def test_short_dropouts_do_not_split_a_tone(self):
        sr = 22_050
        recording = np.concatenate((silence(sr, 1), sine(sr, 4, 1050), silence(sr, 0.2), sine(sr, 4, 1050), silence(sr, 1)))
        self.assert_one_tone(recording, sr, "nwr", 1.0, 9.2)

    def test_ignores_audio_that_is_not_an_attention_tone(self):
        sr = 22_050
        cases = {
            "voice": voice_like(sr, 10),
            "noisy voice": with_noise(voice_like(sr, 10), 0),
            "1 kHz test tone": sine(sr, 10, 1000),
            "noisy 1 kHz test tone": with_noise(sine(sr, 10, 1000), -3),
            "853 Hz alone": sine(sr, 10, 853),
            "960 Hz alone": sine(sr, 10, 960),
            "chord containing 1047 Hz": sine(sr, 10, 523.25, 659.25, 783.99, 1046.5),
            "SAME bursts": np.tile(same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sr), 5),
            "noise": np.random.default_rng(3).normal(0.0, 0.2, sr * 10),
            "1050 Hz blip shorter than the minimum": np.concatenate((silence(sr, 1), sine(sr, 1.5, 1050), silence(sr, 1))),
        }
        for name, recording in cases.items():
            with self.subTest(name):
                self.assertEqual(atd.find_attention_tones(recording, sr), [])

    def test_tracker_reports_tone_end_once_it_stops(self):
        sr = 22_050
        tracker = atd.AttentionToneTracker(sr)
        to_pcm = lambda x: np.clip(x * 32767, -32768, 32767).astype("<i2").tobytes()

        found = tracker.feed(to_pcm(np.concatenate((silence(sr, 1), sine(sr, 5, 1050)))))
        self.assertEqual(len(found), 1)
        self.assertIsNone(found[0].end)
        tracker.feed(to_pcm(silence(sr, 1)))
        self.assertAlmostEqual(found[0].end / sr, 6.0, delta=atd.FRAME_SECONDS + 0.01)


if __name__ == "__main__":
    unittest.main()

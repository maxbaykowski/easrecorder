import importlib.util
import unittest

import numpy as np

from easrecorder import mp3_encoder
from easrecorder.mp3_encoder import Mp3Encoder, Mp3EncoderError
from easrecorder.recorder import MP3_BITRATE_KBPS, MP3_SAMPLE_RATES


def sample_pcm(sample_rate, seconds=3):
    rng = np.random.default_rng(4)
    t = np.arange(sample_rate * seconds) / sample_rate
    audio = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(t.size)) * 32767 * 0.6
    return audio.astype("<i2").tobytes()


def encode(encoder, pcm, chunk_bytes=16384):
    encoded = b"".join(encoder.encode(pcm[i : i + chunk_bytes]) for i in range(0, len(pcm), chunk_bytes))
    return encoded + encoder.flush()


class Mp3EncoderTests(unittest.TestCase):
    def test_writes_constant_bitrate_mono_frames(self):
        with Mp3Encoder(24_000, 64) as encoder:
            encoded = encode(encoder, sample_pcm(24_000, 4))

        # MPEG audio frame header: 11-bit sync, then channel mode 3 (mono) in the 4th byte.
        self.assertEqual(encoded[0], 0xFF)
        self.assertEqual(encoded[1] & 0xE0, 0xE0)
        self.assertEqual(encoded[3] >> 6, 3)
        # Constant 64 kbps for 4 seconds is 32,000 bytes, give or take LAME's start and end.
        self.assertAlmostEqual(len(encoded), 32_000, delta=1_500)

    @unittest.skipUnless(importlib.util.find_spec("lameenc"), "the lameenc Python package is not installed")
    def test_output_is_identical_to_lameenc_it_replaced(self):
        import lameenc

        for sample_rate in MP3_SAMPLE_RATES:
            with self.subTest(sample_rate=sample_rate):
                pcm = sample_pcm(sample_rate)
                with Mp3Encoder(sample_rate, MP3_BITRATE_KBPS) as encoder:
                    native = encode(encoder, pcm)
                # The settings the recorder used with lameenc.
                reference = lameenc.Encoder()
                reference.set_bit_rate(MP3_BITRATE_KBPS)
                reference.set_in_sample_rate(sample_rate)
                reference.set_out_sample_rate(sample_rate)
                reference.set_channels(1)
                reference.set_quality(2)
                expected = encode(reference, pcm)

                self.assertEqual(native, expected)

    def test_can_be_closed_twice_and_refuses_audio_after(self):
        encoder = Mp3Encoder(22_050, MP3_BITRATE_KBPS)
        encoder.encode(sample_pcm(22_050, 1))
        encoder.close()
        encoder.close()
        self.assertEqual(encoder.flush(), b"")
        with self.assertRaises(Mp3EncoderError):
            encoder.encode(sample_pcm(22_050, 1))

    def test_missing_library_gives_a_clear_error(self):
        with self.assertRaisesRegex(Mp3EncoderError, "'no-such-codec' could not be loaded"):
            mp3_encoder._load_shared_library("no-such-codec", ("libno-such-codec.so.0",))


if __name__ == "__main__":
    unittest.main()

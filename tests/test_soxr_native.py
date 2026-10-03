import importlib.util
import unittest

import numpy as np

from easrecorder.soxr_native import SoxrStream

# (input, output) rates the recorder resamples between: stream rates to multimon-ng's
# 22050 Hz, and recording rates to the nearest MPEG rate for MP3 files.
RATE_PAIRS = ((8_000, 22_050), (16_000, 22_050), (44_100, 22_050), (48_000, 22_050), (96_000, 22_050), (240_000, 48_000), (20_000, 22_050))


def stream_through(resample, data, sizes):
    parts, position = [], 0
    for size in sizes:
        parts.append(resample(data[position : position + size], False))
        position += size
    parts.append(resample(data[position:], True))
    return parts


class SoxrNativeTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("soxr"), "the soxr Python package is not installed")
    def test_matches_the_soxr_python_package_it_replaces(self):
        import soxr

        rng = np.random.default_rng(5)
        for input_rate, output_rate in RATE_PAIRS:
            with self.subTest(input_rate=input_rate, output_rate=output_rate):
                pcm = np.clip(0.3 * rng.standard_normal(input_rate * 2) * 32767, -32768, 32767).astype(np.int16)
                # The recorder's 0.1 s chunks, plus some odd sizes.
                chunk = max(2048, input_rate // 10)
                sizes = [chunk] * 10 + [17, 1, 3000, 999]
                package = soxr.ResampleStream(input_rate, output_rate, 1, dtype="int16")
                native = SoxrStream(input_rate, output_rate, "int16")
                expected = stream_through(lambda x, last: package.resample_chunk(x, last=last), pcm, sizes)
                actual = stream_through(lambda x, last: native.process(x, last=last), pcm, sizes)

                # Same pieces at the same time, so the decoder sees no timing change.
                self.assertEqual([part.size for part in actual], [part.size for part in expected])
                np.testing.assert_allclose(
                    np.concatenate(actual).astype(np.float64),
                    np.concatenate(expected).astype(np.float64),
                    rtol=0,
                    atol=1,  # the package's own libsoxr build rounds slightly differently
                )

    def test_16_bit_output_is_the_same_every_time(self):
        # libsoxr's default dither is seeded from the clock and the resampler's address,
        # which would make the same audio come out slightly different every time.
        rng = np.random.default_rng(8)
        pcm = np.clip(rng.standard_normal(48_000) * 6000, -32768, 32767).astype(np.int16)
        first = SoxrStream(48_000, 22_050, "int16")
        second = SoxrStream(48_000, 22_050, "int16")

        np.testing.assert_array_equal(first.process(pcm, last=True), second.process(pcm, last=True))

    def test_resamples_a_tone_cleanly_and_flushes_the_rest(self):
        stream = SoxrStream(48_000, 22_050, "int16")
        tone = (10_000 * np.sin(2 * np.pi * 1050.0 * np.arange(48_000) / 48_000)).astype(np.int16)

        parts = [stream.process(tone[index : index + 4800]) for index in range(0, tone.size, 4800)]
        parts.append(stream.process(np.zeros(0, dtype=np.int16), last=True))
        output = np.concatenate(parts).astype(np.float64)

        self.assertEqual(output.size, 22_050)
        settled = output[2_000:-2_000]
        spectrum = np.abs(np.fft.rfft(settled * np.hanning(settled.size)))
        frequencies = np.fft.rfftfreq(settled.size, 1 / 22_050)
        self.assertAlmostEqual(float(frequencies[np.argmax(spectrum)]), 1050.0, delta=3.0)
        self.assertAlmostEqual(float(np.max(np.abs(settled))), 10_000, delta=50)
        with self.assertRaises(RuntimeError):
            stream.process(tone[:480])

    def test_rejects_bad_settings(self):
        with self.assertRaises(ValueError):
            SoxrStream(0, 22_050, "int16")
        with self.assertRaises(ValueError):
            SoxrStream(48_000, 22_050, "int32")


if __name__ == "__main__":
    unittest.main()

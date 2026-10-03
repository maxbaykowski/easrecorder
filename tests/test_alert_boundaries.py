import io
import tempfile
import unittest
import wave
from pathlib import Path

from easrecorder import EASRecorder, RecorderSettings
from easrecorder.recorder import EOM_WAIT_SECONDS, HEADER_START_MARGIN_SECONDS


HEADER = "ZCZC-WXR-RWT-026081+0030-2751700-KGRR/NWS-"
RATE = 22050


def feed(recorder, audio):
    """Route audio the way write() does, without the multimon-ng process."""
    for offset in range(0, len(audio), recorder.chunk_bytes):
        chunk = audio[offset : offset + recorder.chunk_bytes]
        recorder._detect_preambles(chunk)
        recorder._process_decoded_lines()
        recorder._write_alert_audio(chunk)


def decoded(recorder, *payloads):
    recorder._lines.extend(f"EAS: {payload}" for payload in payloads)
    recorder._process_decoded_lines()


def partial(recorder, header):
    recorder._lines.append(f"EAS (part): {header}")
    recorder._process_decoded_lines()


def tone(recorder, seconds):
    return recorder._tone_bytes([440.0], seconds, amplitude=0.3)


def recorded_audio(outdir):
    with wave.open(str(next(Path(outdir).glob("*.wav"))), "rb") as source:
        return source.readframes(source.getnframes())


def recorded_spans(outdir, stream):
    """Where each recording sits in the stream, as (name, start, end) byte offsets."""
    spans = []
    for path in sorted(Path(outdir).glob("*.wav")):
        with wave.open(str(path), "rb") as source:
            audio = source.readframes(source.getnframes())
        start = stream.find(audio)
        spans.append((path.name, start, start + len(audio)))
    return spans


class AlertBoundaryTests(unittest.TestCase):
    def make_recorder(self, outdir, **settings):
        log = io.StringIO()
        recorder = EASRecorder(RecorderSettings(rate=RATE, outdir=outdir, year=2026, **settings), log_stream=log)
        return recorder, log

    def test_recording_starts_at_first_header_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            burst = recorder._same_burst_bytes(HEADER)
            gap = recorder._silence_bytes(1.0)
            lead_in = tone(recorder, 3.0)
            stream = lead_in + burst + gap + burst
            feed(recorder, stream)
            decoded(recorder, HEADER)
            feed(recorder, gap + burst + tone(recorder, 2.0))
            decoded(recorder, "NNNN", "NNNN", "NNNN")

            audio = recorded_audio(tmp)
            margin = int(HEADER_START_MARGIN_SECONDS * RATE) * 2
            expected_start = len(lead_in) - margin
            self.assertEqual(audio[: len(stream) - expected_start], stream[expected_start:])
            self.assertIn("found by preamble detector", log.getvalue())

    def test_pre_seconds_are_added_before_the_first_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, _ = self.make_recorder(tmp, pre_seconds=1.0)
            burst = recorder._same_burst_bytes(HEADER)
            lead_in = tone(recorder, 3.0)
            feed(recorder, lead_in + burst + recorder._silence_bytes(1.0) + burst)
            decoded(recorder, HEADER, "NNNN", "NNNN", "NNNN")

            audio = recorded_audio(tmp)
            margin = int(HEADER_START_MARGIN_SECONDS * RATE) * 2
            self.assertEqual(audio[:100], lead_in[len(lead_in) - margin - RATE * 2 :][:100])

    def test_falls_back_to_first_decoded_burst_without_preambles(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            recorder._detect_preambles = lambda audio: None
            feed(recorder, tone(recorder, 6.0))
            recorder._lines.append(f"EAS (part): {HEADER}")
            feed(recorder, tone(recorder, 2.0))
            decoded(recorder, HEADER, "NNNN", "NNNN", "NNNN")

            self.assertIn("found by first decoded burst", log.getvalue())
            self.assertGreater(len(recorded_audio(tmp)), 2 * RATE * 2)

    def test_recording_continues_until_third_eom(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            decoded(recorder, HEADER)
            body = tone(recorder, 1.0)
            between = recorder._silence_bytes(1.0)
            feed(recorder, body)
            decoded(recorder, "NNNN")
            feed(recorder, between)
            decoded(recorder, "NNNN")
            self.assertTrue(recorder._recording)
            feed(recorder, between)
            decoded(recorder, "NNNN")

            self.assertFalse(recorder._recording)
            self.assertEqual(recorded_audio(tmp), body + between + between)
            self.assertIn("STOP: EOM", log.getvalue())

    def test_ends_at_last_eom_when_others_are_missed(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            decoded(recorder, HEADER)
            body = tone(recorder, 1.0)
            feed(recorder, body)
            decoded(recorder, "NNNN")
            feed(recorder, tone(recorder, EOM_WAIT_SECONDS - 0.5))
            self.assertTrue(recorder._recording)
            feed(recorder, tone(recorder, 1.0))

            self.assertFalse(recorder._recording)
            self.assertEqual(recorded_audio(tmp), body)
            self.assertIn("STOP: EOM (1 of 3 heard)", log.getvalue())

    def test_new_header_after_eom_starts_from_its_first_burst(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, _ = self.make_recorder(tmp)
            decoded(recorder, HEADER)
            feed(recorder, tone(recorder, 1.0))
            decoded(recorder, "NNNN")
            burst = recorder._same_burst_bytes("ZCZC-WXR-SVR-026081+0030-2751701-KGRR/NWS-")
            feed(recorder, recorder._silence_bytes(0.5) + burst)
            decoded(recorder, "ZCZC-WXR-SVR-026081+0030-2751701-KGRR/NWS-")
            decoded(recorder, "NNNN", "NNNN", "NNNN")

            files = sorted(Path(tmp).glob("*.wav"))
            self.assertEqual([path.name[:3] for path in files], ["RWT", "SVR"])
            with wave.open(str(files[1]), "rb") as source:
                self.assertGreater(source.getnframes(), len(burst) // 2)

    def send_header(self, recorder, header, stream):
        """Feed a three-burst header; the decoder reports each burst as it ends."""
        burst = recorder._same_burst_bytes(header)
        gap = recorder._silence_bytes(1.0)
        first_burst = len(stream)
        for _ in range(3):
            feed(recorder, burst)
            partial(recorder, header)
            feed(recorder, gap)
            stream += burst + gap
        return stream, first_burst

    def test_back_to_back_alerts_split_at_the_next_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp, pre_seconds=3.0, post_seconds=5.0)
            other = "ZCZC-WXR-SVS-026081+0030-2751702-KGRR/NWS-"
            stream = tone(recorder, 4.0)
            feed(recorder, stream)
            stream, _ = self.send_header(recorder, HEADER, stream)
            body = tone(recorder, 3.0)
            feed(recorder, body)
            stream += body
            for _ in range(3):
                eom = recorder._same_burst_bytes("NNNN") + recorder._silence_bytes(1.0)
                feed(recorder, eom)
                decoded(recorder, "NNNN")
                stream += eom
            stream, next_header = self.send_header(recorder, other, stream)
            body = tone(recorder, 2.0)
            feed(recorder, body)
            stream += body
            recorder.stop("input EOF")

            spans = dict((name[:3], (start, end)) for name, start, end in recorded_spans(tmp, stream))
            self.assertEqual(spans["RWT"][1], spans["SVS"][0])
            self.assertLess(abs(spans["SVS"][0] - next_header), recorder._preamble_tracker.frame_samples * 2 + 4410)
            self.assertIn("STOP: EOM, next alert followed", log.getvalue())

    def test_header_in_the_middle_of_an_alert_ends_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            other = "ZCZC-WXR-SVS-026081+0030-2751702-KGRR/NWS-"
            stream = tone(recorder, 2.0)
            feed(recorder, stream)
            stream, first_header = self.send_header(recorder, HEADER, stream)
            body = tone(recorder, 5.0)
            feed(recorder, body)
            stream += body
            stream, interrupting_header = self.send_header(recorder, other, stream)
            recorder.stop("input EOF")

            spans = dict((name[:3], (start, end)) for name, start, end in recorded_spans(tmp, stream))
            tolerance = recorder._preamble_tracker.frame_samples * 2 + 4410
            self.assertLess(abs(spans["RWT"][0] - first_header), tolerance)
            self.assertEqual(spans["RWT"][1], spans["SVS"][0])
            self.assertLess(abs(spans["SVS"][0] - interrupting_header), tolerance)
            self.assertIn("STOP: interrupted by a new header", log.getvalue())

    def test_repeated_identical_header_starts_a_second_recording(self):
        # multimon-ng does not print a confirmed header identical to the last one,
        # so the second alert is only seen through its partial decodes.
        with tempfile.TemporaryDirectory() as tmp:
            recorder, _ = self.make_recorder(tmp)
            stream = tone(recorder, 2.0)
            feed(recorder, stream)
            stream, _ = self.send_header(recorder, HEADER, stream)
            decoded(recorder, HEADER)
            feed(recorder, tone(recorder, 3.0))
            decoded(recorder, "NNNN", "NNNN", "NNNN")
            self.send_header(recorder, HEADER, stream)
            recorder.stop("input EOF")

            names = sorted(path.name for path in Path(tmp).glob("*.wav"))
            self.assertEqual(len(names), 2, names)
            self.assertTrue(names[1].endswith("UTC.wav") and names[0].endswith("UTC-2.wav"), names)

    def test_confirmed_line_after_partials_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            self.send_header(recorder, HEADER, tone(recorder, 1.0))
            decoded(recorder, HEADER)
            recorder.stop("input EOF")

            self.assertEqual(log.getvalue().count("START:"), 1)


if __name__ == "__main__":
    unittest.main()

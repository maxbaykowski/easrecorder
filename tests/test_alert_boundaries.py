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
        recorder._detect_attention_tones(chunk)
        recorder._process_decoded_lines()
        recorder._write_alert_audio(chunk)


def decoded(recorder, *payloads):
    recorder._lines.extend(f"EAS: {payload}" for payload in payloads)
    recorder._process_decoded_lines()


def partial(recorder, header):
    recorder._lines.append(f"EAS (part): {header}")
    recorder._process_decoded_lines()


def send_header(recorder, header, stream):
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

    def test_back_to_back_alerts_split_at_the_next_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp, pre_seconds=3.0, post_seconds=5.0)
            other = "ZCZC-WXR-SVS-026081+0030-2751702-KGRR/NWS-"
            stream = tone(recorder, 4.0)
            feed(recorder, stream)
            stream, _ = send_header(recorder, HEADER, stream)
            body = tone(recorder, 3.0)
            feed(recorder, body)
            stream += body
            for _ in range(3):
                eom = recorder._same_burst_bytes("NNNN") + recorder._silence_bytes(1.0)
                feed(recorder, eom)
                decoded(recorder, "NNNN")
                stream += eom
            stream, next_header = send_header(recorder, other, stream)
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
            stream, first_header = send_header(recorder, HEADER, stream)
            body = tone(recorder, 5.0)
            feed(recorder, body)
            stream += body
            stream, interrupting_header = send_header(recorder, other, stream)
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
            stream, _ = send_header(recorder, HEADER, stream)
            decoded(recorder, HEADER)
            feed(recorder, tone(recorder, 3.0))
            decoded(recorder, "NNNN", "NNNN", "NNNN")
            send_header(recorder, HEADER, stream)
            recorder.stop("input EOF")

            names = sorted(path.name for path in Path(tmp).glob("*.wav"))
            self.assertEqual(len(names), 2, names)
            self.assertTrue(names[1].endswith("UTC.wav") and names[0].endswith("UTC-2.wav"), names)

    def test_confirmed_line_after_partials_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, log = self.make_recorder(tmp)
            send_header(recorder, HEADER, tone(recorder, 1.0))
            decoded(recorder, HEADER)
            recorder.stop("input EOF")

            self.assertEqual(log.getvalue().count("START:"), 1)

    def record_with_tone(self, tmp, tone_audio, max_seconds=5):
        recorder, log = self.make_recorder(tmp, max_seconds=max_seconds)
        stream = tone(recorder, 1.0)
        feed(recorder, stream)
        stream, first_header = send_header(recorder, HEADER, stream)
        stream += tone_audio
        feed(recorder, tone_audio)
        rest = recorder._tone_bytes([440.0, 660.0], 20.0, amplitude=0.3)
        feed(recorder, rest)
        stream += rest
        recorder.stop("input EOF")
        name, start, end = recorded_spans(tmp, stream)[0]
        return log.getvalue(), start, end

    def test_max_seconds_starts_after_nwr_attention_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, _ = self.make_recorder(tmp)
            gap = recorder._silence_bytes(2.0)
            attention = recorder._tone_bytes([1050.0], 9.0)
            log, start, end = self.record_with_tone(tmp, gap + attention)

            # The header ended 2 s before the tone; the file runs 5 s past the tone.
            tone_end = end - 5 * RATE * 2
            self.assertIn("Attention tone 1050 Hz lasted 9.", log)
            self.assertIn("STOP: timeout", log)
            self.assertLess(abs((tone_end - start) / (RATE * 2) - (0.05 + 3 * 1.89 + 2.0 + 9.0)), 0.2)

    def test_max_seconds_starts_after_ebs_attention_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, _ = self.make_recorder(tmp)
            attention = recorder._tone_bytes([853.0, 960.0], 9.0, amplitude=0.7)
            log, start, end = self.record_with_tone(tmp, recorder._silence_bytes(3.0) + attention)

            self.assertIn("Attention tone 853 + 960 Hz lasted 9.", log)
            self.assertGreater((end - start) / (RATE * 2), 3 * 1.89 + 3.0 + 9.0 + 4.8)

    def test_max_seconds_starts_at_header_without_attention_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, start, end = self.record_with_tone(tmp, b"")

            self.assertIn("No attention tone", log)
            # Confirmed after the second burst (no decoder lag here), plus 5 s.
            self.assertLess(abs((end - start) / (RATE * 2) - (0.05 + 1.89 + 0.89 + 5.0)), 0.2)

    def test_tone_starting_long_after_the_header_is_not_the_attention_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, _ = self.make_recorder(tmp)
            late = recorder._silence_bytes(9.0) + recorder._tone_bytes([1050.0], 9.0)
            log, _, _ = self.record_with_tone(tmp, late, max_seconds=30)

            self.assertIn("No attention tone", log)


class ReconstructionTests(unittest.TestCase):
    """Reconstruction mode keeps only the live alert audio between generated SAME audio."""

    def run_alert(self, tmp, after_header, body_seconds=6.0, eoms=True, max_seconds=120):
        log = io.StringIO()
        recorder = EASRecorder(
            RecorderSettings(rate=RATE, outdir=tmp, year=2026, reconstruct_same=True, max_seconds=max_seconds),
            log_stream=log,
        )
        stream = tone(recorder, 2.0)
        feed(recorder, stream)
        stream, _ = send_header(recorder, HEADER, stream)
        header_end = len(stream) - len(recorder._silence_bytes(1.0))
        stream += after_header
        feed(recorder, after_header)
        live_start = len(stream)
        body = recorder._tone_bytes([440.0, 660.0], body_seconds, amplitude=0.3)
        stream += body
        feed(recorder, body)
        eom_start = len(stream)
        if eoms:
            eom = recorder._same_burst_bytes("NNNN")
            feed(recorder, eom)
            decoded(recorder, "NNNN")
            stream += eom
        tail = tone(recorder, 2.0)
        feed(recorder, tail)
        stream += tail
        recorder.stop("input EOF")

        with wave.open(str(next(Path(tmp).glob("*.wav"))), "rb") as source:
            audio = source.readframes(source.getnframes())
        alert = recorder.snapshot_alert_settings()
        prefix = len(recorder._reconstructed_prefix_bytes(HEADER, alert))
        suffix = len(recorder._reconstructed_suffix_bytes())
        live = audio[prefix : len(audio) - suffix]
        start = stream.find(live)
        self.assertGreaterEqual(start, 0)
        return log.getvalue(), start, start + len(live), header_end, live_start, eom_start

    def assert_near(self, position, expected, seconds=0.12):
        self.assertLess(abs(position - expected), seconds * RATE * 2, (position / (RATE * 2), expected / (RATE * 2)))

    def test_live_audio_starts_after_the_attention_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = EASRecorder(RecorderSettings(rate=RATE), log_stream=io.StringIO())
            after_header = helper._silence_bytes(2.0) + helper._tone_bytes([1050.0], 9.0)
            log, start, end, _, live_start, eom_start = self.run_alert(tmp, after_header)

            self.assertIn("Attention tone 1050 Hz", log)
            self.assert_near(start, live_start)
            self.assert_near(end, eom_start - HEADER_START_MARGIN_SECONDS * RATE * 2, 0.03)

    def test_live_audio_starts_after_third_header_without_a_tone(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = EASRecorder(RecorderSettings(rate=RATE), log_stream=io.StringIO())
            log, start, end, header_end, _, eom_start = self.run_alert(tmp, helper._silence_bytes(1.0))

            self.assertIn("No attention tone", log)
            self.assert_near(start, header_end, 0.05)
            self.assert_near(end, eom_start - HEADER_START_MARGIN_SECONDS * RATE * 2, 0.03)

    def test_max_seconds_counts_from_the_start_of_live_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            helper = EASRecorder(RecorderSettings(rate=RATE), log_stream=io.StringIO())
            after_header = helper._silence_bytes(2.0) + helper._tone_bytes([1050.0], 9.0)
            log, start, end, _, _, _ = self.run_alert(tmp, after_header, body_seconds=20.0, eoms=False, max_seconds=5)

            self.assertIn("STOP: timeout", log)
            self.assertEqual(end - start, 5 * RATE * 2)

    def test_new_header_ends_the_live_audio_where_it_began(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = EASRecorder(
                RecorderSettings(rate=RATE, outdir=tmp, year=2026, reconstruct_same=True),
                log_stream=io.StringIO(),
            )
            stream = tone(recorder, 2.0)
            feed(recorder, stream)
            stream, _ = send_header(recorder, HEADER, stream)
            body = tone(recorder, 12.0)
            feed(recorder, body)
            stream += body
            stream, interrupting = send_header(
                recorder, "ZCZC-WXR-SVS-026081+0030-2751702-KGRR/NWS-", stream
            )
            recorder.stop("input EOF")

            first = min(Path(tmp).glob("RWT*.wav"))
            with wave.open(str(first), "rb") as source:
                audio = source.readframes(source.getnframes())
            alert = recorder.snapshot_alert_settings()
            live = audio[len(recorder._reconstructed_prefix_bytes(HEADER, alert)) : -len(recorder._reconstructed_suffix_bytes())]
            end = stream.find(live) + len(live)
            self.assertLess(abs(end - interrupting), 0.08 * RATE * 2)
            self.assertEqual(len(list(Path(tmp).glob("*.wav"))), 2)


if __name__ == "__main__":
    unittest.main()

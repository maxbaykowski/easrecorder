import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from easrecorder import EASRecorder, RecorderSettings, parse_same_header


HEADER = "ZCZC-WXR-TOR-039173-039051-139069+0030-1591829-KCLE/NWS-"


class SameHeaderTests(unittest.TestCase):
    def test_parses_canonical_same_header(self):
        parsed = parse_same_header(HEADER, year=2026)

        self.assertEqual(parsed.raw_header, HEADER)
        self.assertEqual(parsed.event_type, "TOR")
        self.assertEqual(parsed.originator, "WXR")
        self.assertEqual(parsed.fips_codes, ("039173", "039051", "139069"))
        self.assertEqual(parsed.start_time_utc, datetime(2026, 6, 8, 18, 29, tzinfo=timezone.utc))
        self.assertEqual(parsed.duration_code, "0030")
        self.assertEqual(parsed.duration_seconds, 1800)
        self.assertEqual(parsed.sender_id, "KCLE/NWS")

    def test_preserves_unknown_event_and_originator_codes(self):
        parsed = parse_same_header(
            "ZCZC-XYZ-ABC-999000+0100-0010000-UNKNOWN1-",
            year=2026,
        )

        self.assertEqual(parsed.originator, "XYZ")
        self.assertEqual(parsed.event_type, "ABC")
        self.assertEqual(parsed.fips_codes, ("999000",))

    def test_malformed_header_uses_safe_fallbacks(self):
        now = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        parsed = parse_same_header("ZCZC-WXR-", now=now)

        self.assertEqual(parsed.originator, "WXR")
        self.assertEqual(parsed.event_type, "UNK")
        self.assertEqual(parsed.fips_codes, ())
        self.assertEqual(parsed.start_time_utc, datetime(2026, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(parsed.sender_id, "UNKNOWN")

    def test_invalid_non_leap_julian_day_uses_current_time(self):
        now = datetime(2026, 2, 3, 4, 5, tzinfo=timezone.utc)
        parsed = parse_same_header(
            "ZCZC-WXR-RWT-026081+0030-3661200-KGRR/NWS-",
            year=2026,
            now=now,
        )

        self.assertEqual(parsed.start_time_utc, now)


class AlertIndexTests(unittest.TestCase):
    def test_recording_lifecycle_writes_index_for_finalized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = RecorderSettings(
                rate=22050,
                outdir=tmp,
                year=2026,
                index_path="index.json",
            )
            recorder = EASRecorder(settings, log_stream=io.StringIO())
            recorder._lines.append(f"EAS: {HEADER}")
            recorder._process_decoded_lines()
            recorder._write_alert_audio(b"\x00\x00" * 100)
            recorder._lines.append("EAS: NNNN")
            recorder._process_decoded_lines()

            data = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
            entry = data["alerts"][0]
            self.assertEqual(entry["raw_same_header"], HEADER)
            self.assertTrue(Path(entry["file_path"]).is_absolute())
            self.assertTrue(Path(entry["file_path"]).exists())

    def test_mp3_index_waits_for_conversion_and_uses_final_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = RecorderSettings(
                rate=22050,
                outdir=tmp,
                year=2026,
                save_format="mp3",
                index_path="index.json",
            )
            recorder = EASRecorder(settings, log_stream=io.StringIO())
            recorder._lines.append(f"EAS: {HEADER}")
            recorder._process_decoded_lines()
            recorder._write_alert_audio(b"\x00\x00" * 2205)
            recorder._lines.append("EAS: NNNN")
            recorder._process_decoded_lines()
            for conversion in recorder._mp3_threads:
                conversion.join()

            data = json.loads((Path(tmp) / "index.json").read_text(encoding="utf-8"))
            file_path = Path(data["alerts"][0]["file_path"])
            self.assertEqual(file_path.suffix, ".mp3")
            self.assertTrue(file_path.exists())

    def test_merges_prior_index_and_sorts_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            old_entry = {
                "event_type": "RWT",
                "start_time_utc": "2026-01-01T00:00:00Z",
                "file_path": "/old.wav",
            }
            index_path.write_text(json.dumps({"version": 1, "alerts": [old_entry]}), encoding="utf-8")
            settings = RecorderSettings(rate=22050, outdir=tmp, index_path="index.json")
            recorder = EASRecorder(settings, log_stream=io.StringIO())
            alert = recorder.snapshot_alert_settings()
            parsed = parse_same_header(HEADER, year=2026)
            output_path = Path(tmp) / "alert.wav"

            recorder._write_index_entry(alert, parsed, str(output_path))

            data = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1)
            self.assertEqual([item["event_type"] for item in data["alerts"]], ["TOR", "RWT"])
            self.assertEqual(data["alerts"][0]["file_path"], str(output_path.resolve()))
            self.assertEqual(data["alerts"][0]["expires_at_utc"], "2026-06-08T18:59:00Z")

    def test_accepts_legacy_top_level_alert_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.json"
            index_path.write_text("[]", encoding="utf-8")
            settings = RecorderSettings(rate=22050, outdir=tmp, index_path="index.json")
            recorder = EASRecorder(settings, log_stream=io.StringIO())

            recorder._write_index_entry(
                recorder.snapshot_alert_settings(),
                parse_same_header(HEADER, year=2026),
                str(Path(tmp) / "alert.wav"),
            )

            data = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], 1)
            self.assertEqual(len(data["alerts"]), 1)

    def test_index_can_be_disabled_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = RecorderSettings(rate=22050, outdir=tmp, index_path="index.json")
            recorder = EASRecorder(settings, log_stream=io.StringIO())
            enabled_alert = recorder.snapshot_alert_settings()
            settings.index_path = None
            disabled_alert = recorder.snapshot_alert_settings()
            parsed = parse_same_header(HEADER, year=2026)

            recorder._write_index_entry(disabled_alert, parsed, str(Path(tmp) / "disabled.wav"))
            self.assertFalse((Path(tmp) / "index.json").exists())

            recorder._write_index_entry(enabled_alert, parsed, str(Path(tmp) / "enabled.wav"))
            self.assertTrue((Path(tmp) / "index.json").exists())


if __name__ == "__main__":
    unittest.main()

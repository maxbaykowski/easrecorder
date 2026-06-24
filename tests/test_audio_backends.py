import io
import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import easrecorder.recorder as recorder_module
from easrecorder import EASRecorder, RecorderSettings


class _FakePipe:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)
        return len(data)

    def close(self):
        pass


class _FakeStdout:
    def readline(self):
        return b""


class _FakeProcess:
    def __init__(self, *args, **kwargs):
        self.stdin = _FakePipe()
        self.stdout = _FakeStdout()

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


class AudioBackendTests(unittest.TestCase):
    def test_detector_audio_is_stream_resampled(self):
        with patch.object(recorder_module.subprocess, "Popen", _FakeProcess):
            recorder = EASRecorder(
                RecorderSettings(rate=44100, detect_rate=22050),
                log_stream=io.StringIO(),
            )
            recorder.start()
            process = recorder._mm
            recorder.write(b"\x00\x00" * 44100)
            recorder.stop()

        self.assertEqual(len(process.stdin.data) // 2, 22050)

    def test_mp3_encoding_resamples_unsupported_source_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "alert.wav"
            source_rate = 240000
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(source_rate)
                frames = bytearray()
                for index in range(source_rate // 10):
                    sample = int(12000 * math.sin(2 * math.pi * 1000 * index / source_rate))
                    frames.extend(sample.to_bytes(2, "little", signed=True))
                output.writeframes(frames)

            recorder = EASRecorder(
                RecorderSettings(rate=source_rate),
                log_stream=io.StringIO(),
            )
            recorder._convert_to_mp3(str(wav_path))

            mp3_path = wav_path.with_suffix(".mp3")
            self.assertTrue(mp3_path.exists())
            self.assertGreater(mp3_path.stat().st_size, 0)
            self.assertFalse(wav_path.exists())


if __name__ == "__main__":
    unittest.main()

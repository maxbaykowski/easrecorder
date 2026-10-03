import io
import math
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import easrecorder.recorder as recorder_module
from easrecorder import EASRecorder, RecorderSettings
from easrecorder.mp3_encoder import Mp3EncoderError
from easrecorder.soxr_native import soxr_library


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
        # Finding libsoxr runs a subprocess of its own (ctypes.util.find_library), so
        # load it before Popen is replaced with the fake decoder.
        soxr_library()
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

    def test_missing_libmp3lame_keeps_the_wav_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "alert.wav"
            with wave.open(str(wav_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(22050)
                output.writeframes(b"\x00\x00" * 22050)
            log = io.StringIO()
            recorder = EASRecorder(RecorderSettings(rate=22050), log_stream=log)

            def missing(*args, **kwargs):
                raise Mp3EncoderError("required shared library 'mp3lame' could not be loaded")

            with patch.object(recorder_module, "Mp3Encoder", missing):
                recorder._convert_to_mp3(str(wav_path))

            self.assertTrue(wav_path.exists())
            self.assertEqual(list(Path(tmp).glob("*.mp3*")), [])
            self.assertIn("MP3 conversion failed (required shared library 'mp3lame' could not be loaded)", log.getvalue())


if __name__ == "__main__":
    unittest.main()

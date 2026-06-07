from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, TextIO


MAX_PRERECORD_SECONDS = 10.0
MAX_POSTRECORD_SECONDS = 10.0
SAVE_FORMATS = {"wav", "mp3"}


@dataclass
class RecorderSettings:
    rate: int
    detect_rate: int = 22050
    outdir: str = "."
    max_seconds: int = 120
    prefix: str | None = None
    save_format: str = "wav"
    local_time: bool = False
    reconstruct_same: bool = False
    tone: str = "none"
    tone_duration: float | None = None
    year: int | None = None
    pre_seconds: float = 0.0
    post_seconds: float = 0.0
    copy_stdout: bool = False


@dataclass(frozen=True)
class _AlertSettings:
    outdir: str
    max_bytes: int
    prefix: str | None
    save_format: str
    local_time: bool
    reconstruct_same: bool
    tone: str
    tone_duration: float
    name_year: int
    pre_bytes: int
    post_bytes: int


def _reader_thread(proc: subprocess.Popen, q: deque[str]) -> None:
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        q.append(line.decode("utf-8", errors="ignore").rstrip("\n"))


class EASRecorder:
    """Record EAS alerts from raw signed 16-bit little-endian mono PCM.

    Use ``run()`` to read from a stream until EOF, or use ``start()``,
    ``write()``, and ``stop()`` when another Python component produces samples.
    Alert-scoped settings are snapshotted when a new SAME header starts.
    """

    def __init__(
        self,
        settings: RecorderSettings,
        input_stream: BinaryIO | None = None,
        output_stream: BinaryIO | None = None,
        log_stream: TextIO | None = None,
    ) -> None:
        self.settings = settings
        self.input_stream = input_stream if input_stream is not None else sys.stdin.buffer
        self.output_stream = output_stream if output_stream is not None else sys.stdout.buffer
        self.log_stream = log_stream if log_stream is not None else (
            sys.stderr if settings.copy_stdout else sys.stdout
        )

        self._ffmpeg: subprocess.Popen | None = None
        self._mm: subprocess.Popen | None = None
        self._lines: deque[str] = deque()
        self._reader: threading.Thread | None = None
        self._started = False

        self._recording = False
        self._wav = None
        self._cur_path: str | None = None
        self._rec_bytes = 0
        self._post_remaining: int | None = None
        self._post_written = 0
        self._capture_skip_bytes = 0
        self._capture_live_audio = False
        self._record_buf = bytearray()
        self._decode_pace_start: float | None = None
        self._decode_pace_bytes = 0
        self._mp3_threads: list[threading.Thread] = []
        self._pre_buf: deque[bytes] = deque()
        self._pre_buf_bytes = 0
        self._active_alert: _AlertSettings | None = None

        self._bytes_per_second = settings.rate * 2
        self._pre_buf_max_bytes = int(MAX_PRERECORD_SECONDS * self._bytes_per_second)
        self._capture_delay_seconds = 10.0
        self._capture_delay_bytes = int(round(self._capture_delay_seconds * self._bytes_per_second))
        self._trim_tail_bytes = self._bytes_per_second + 16000
        self._decode_pace_slack_seconds = 0.25
        self.chunk_bytes = max(4096, int(settings.rate * 0.1) * 2)

        self._validate_static_settings()

    def start(self) -> None:
        """Start the decoder pipeline.

        Call this before ``write()`` when feeding samples directly. ``run()``
        calls it automatically.
        """

        if self._started:
            return
        self._validate_static_settings()
        os.makedirs(self.settings.outdir, exist_ok=True)

        self._ffmpeg = subprocess.Popen(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(self.settings.rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                str(self.settings.detect_rate),
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

        self._mm = subprocess.Popen(
            ["multimon-ng", "-t", "raw", "-a", "EAS", "-f", str(self.settings.detect_rate), "-"],
            stdin=self._ffmpeg.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._lines.clear()
        self._reader = threading.Thread(target=_reader_thread, args=(self._mm, self._lines), daemon=True)
        self._reader.start()
        self._started = True

        self._log(f"[same] Input rate={self.settings.rate} Hz, detect rate={self.settings.detect_rate} Hz")
        if self.settings.reconstruct_same:
            tone_duration = 10.0 if self.settings.tone_duration is None else self.settings.tone_duration
            self._log(
                f"[same] Reconstruction mode enabled, "
                f"tone={self.settings.tone}, tone_duration={tone_duration:.2f}s"
            )

    def write(self, samples: bytes) -> None:
        """Feed raw PCM samples to the recorder.

        ``samples`` must be signed 16-bit little-endian mono PCM at
        ``settings.rate``.
        """

        if not self._started:
            raise RuntimeError("recorder must be started before write()")
        if not samples:
            return

        audio = samples
        if self.settings.copy_stdout:
            try:
                self.output_stream.write(audio)
                flush = getattr(self.output_stream, "flush", None)
                if flush is not None:
                    flush()
            except BrokenPipeError as exc:
                raise RuntimeError("stdout pipeline exited") from exc

        try:
            assert self._ffmpeg is not None
            assert self._ffmpeg.stdin is not None
            self._ffmpeg.stdin.write(audio)
        except BrokenPipeError as exc:
            raise RuntimeError("ffmpeg pipeline exited") from exc

        if self._active_alert is not None and self._active_alert.reconstruct_same and self._recording:
            self._pace_reconstruction_decode(len(audio))

        self._process_decoded_lines()
        self._write_alert_audio(audio)

    def stop(self, reason: str = "shutdown") -> None:
        """Stop the recorder and close any active alert."""

        if self._recording:
            try:
                self._stop_record(reason)
            except Exception:
                pass
        try:
            if self._ffmpeg is not None and self._ffmpeg.stdin is not None:
                self._ffmpeg.stdin.close()
        except Exception:
            pass
        for proc in (self._mm, self._ffmpeg):
            if proc is None:
                continue
            try:
                proc.terminate()
            except Exception:
                pass
        for t_mp3 in self._mp3_threads:
            try:
                t_mp3.join()
            except Exception:
                pass
        self._started = False
        self._ffmpeg = None
        self._mm = None
        self._reader = None

    def run(self) -> None:
        """Read samples from ``input_stream`` until EOF."""

        shutdown_reason = None
        self.start()
        try:
            while True:
                audio = self.input_stream.read(self.chunk_bytes)
                if not audio:
                    shutdown_reason = "input EOF"
                    break
                try:
                    self.write(audio)
                except RuntimeError as exc:
                    shutdown_reason = str(exc)
                    break
        except KeyboardInterrupt:
            shutdown_reason = "ctrl+c"
        finally:
            self.stop(shutdown_reason or "shutdown")

    def snapshot_alert_settings(self) -> _AlertSettings:
        settings = self.settings
        self._validate_alert_settings()
        now_for_year = datetime.now() if settings.local_time else datetime.now(timezone.utc)
        current_year = now_for_year.year
        name_year = settings.year if settings.year is not None else current_year
        tone_duration = 10.0 if settings.tone_duration is None else settings.tone_duration
        return _AlertSettings(
            outdir=settings.outdir,
            max_bytes=int(max(0, settings.max_seconds) * settings.rate * 2),
            prefix=settings.prefix,
            save_format=settings.save_format,
            local_time=settings.local_time,
            reconstruct_same=settings.reconstruct_same,
            tone=settings.tone,
            tone_duration=tone_duration,
            name_year=name_year,
            pre_bytes=int(max(0.0, min(MAX_PRERECORD_SECONDS, settings.pre_seconds)) * settings.rate * 2),
            post_bytes=int(max(0.0, min(MAX_POSTRECORD_SECONDS, settings.post_seconds)) * settings.rate * 2),
        )

    def _log(self, msg: str) -> None:
        print(msg, file=self.log_stream, flush=True)

    def _process_decoded_lines(self) -> None:
        while self._lines:
            line = self._lines.popleft()
            if "EAS:" not in line:
                continue
            payload = line.split("EAS:", 1)[1].strip()

            if payload.startswith("ZCZC"):
                if self.settings.reconstruct_same:
                    if not self._recording:
                        self._start_reconstructed_record(payload)
                else:
                    if self._recording and self._post_remaining is not None:
                        self._stop_record("EOM superseded")
                        self._start_record(payload, allow_prerec=False)
                    elif not self._recording:
                        self._start_record(payload)

            if payload.startswith("NNNN") and self._recording:
                if self._active_alert is not None and self._active_alert.reconstruct_same:
                    self._stop_record("EOM", saw_eom=True)
                else:
                    post_bytes = self._active_alert.post_bytes if self._active_alert is not None else 0
                    if post_bytes > 0:
                        self._post_remaining = post_bytes
                        self._post_written = 0
                    else:
                        self._stop_record("EOM")

    def _write_alert_audio(self, audio: bytes) -> None:
        if self._active_alert is not None and self._active_alert.reconstruct_same:
            if self._recording and self._wav is not None:
                if not self._capture_live_audio:
                    if self._capture_skip_bytes >= len(audio):
                        self._capture_skip_bytes -= len(audio)
                        audio = b""
                    else:
                        if self._capture_skip_bytes > 0:
                            audio = audio[self._capture_skip_bytes:]
                            self._capture_skip_bytes = 0
                        self._capture_live_audio = True
                        self._log("[same] Capturing alert audio")
                if self._capture_live_audio and audio:
                    self._record_buf.extend(audio)
                    self._rec_bytes += len(audio)
                    if self._active_alert.max_bytes > 0 and self._rec_bytes >= self._active_alert.max_bytes:
                        self._stop_record("timeout")
        elif self._recording and self._wav is not None:
            self._wav.writeframes(audio)
            self._rec_bytes += len(audio)
            if self._active_alert is not None and self._active_alert.max_bytes > 0:
                if self._rec_bytes >= self._active_alert.max_bytes:
                    self._stop_record("timeout")
                    return
            if self._post_remaining is not None:
                self._post_written += len(audio)
                self._post_remaining -= len(audio)
                if self._post_remaining <= 0:
                    self._stop_record("post")
        else:
            self._pre_buf.append(audio)
            self._pre_buf_bytes += len(audio)
            while self._pre_buf_bytes > self._pre_buf_max_bytes:
                dropped = self._pre_buf.popleft()
                self._pre_buf_bytes -= len(dropped)

    def _pace_reconstruction_decode(self, byte_count: int) -> None:
        if self._decode_pace_start is None:
            return
        self._decode_pace_bytes += byte_count
        fed_seconds = self._decode_pace_bytes / self._bytes_per_second
        elapsed_seconds = time.monotonic() - self._decode_pace_start
        ahead_seconds = fed_seconds - elapsed_seconds
        if ahead_seconds > self._decode_pace_slack_seconds:
            time.sleep(ahead_seconds - self._decode_pace_slack_seconds)

    def _parse_event_and_timestamp(self, header_line: str, alert: _AlertSettings):
        event = "UNK"
        now = datetime.now() if alert.local_time else datetime.now(timezone.utc)
        date_str = now.strftime("%m-%d-") + f"{alert.name_year:04d}"
        time_str = now.strftime("%H%M")
        tz_str = now.tzname() or ("UTC" if not alert.local_time else "LOCAL")
        if not header_line.startswith("ZCZC"):
            return event, date_str, time_str, tz_str
        parts = header_line.split("-")
        if len(parts) >= 3:
            event = parts[2] or event
        jjj_match = re.search(r"-(\d{7})-", header_line)
        if jjj_match:
            jjjhhmm = jjj_match.group(1)
            try:
                jjj = int(jjjhhmm[:3])
                hhmm = jjjhhmm[3:7]
                hh = int(hhmm[:2])
                mm = int(hhmm[2:4])
                dt_utc = datetime(alert.name_year, 1, 1, hh, mm, tzinfo=timezone.utc) + timedelta(days=jjj - 1)
                dt = dt_utc.astimezone() if alert.local_time else dt_utc
                date_str = dt.strftime("%m-%d-%Y")
                time_str = dt.strftime("%H%M")
                tz_str = dt.tzname() or ("UTC" if not alert.local_time else "LOCAL")
            except Exception:
                pass
        return event, date_str, time_str, tz_str

    def _build_output_path(self, header_line: str, alert: _AlertSettings) -> str:
        event, date_str, time_str, tz_str = self._parse_event_and_timestamp(header_line, alert)
        if alert.prefix:
            base = f"{alert.prefix}-{event}-{date_str}-{time_str}{tz_str}"
        else:
            base = f"{event}-{date_str}-{time_str}{tz_str}"
        return os.path.join(alert.outdir, f"{base}.wav")

    def _open_output_wav(self, header_line: str, alert: _AlertSettings):
        os.makedirs(alert.outdir, exist_ok=True)
        path = self._build_output_path(header_line, alert)
        self._cur_path = path
        out = wave.open(path, "wb")
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(self.settings.rate)
        self._log(f"[same] START: {header_line}")
        if alert.save_format == "mp3":
            self._log(f"[same] Writing temp WAV: {path}")
        else:
            self._log(f"[same] Writing: {path}")
        return out

    def _start_record(self, header_line: str, allow_prerec: bool = True) -> None:
        self._active_alert = self.snapshot_alert_settings()
        self._wav = self._open_output_wav(header_line, self._active_alert)
        self._recording = True
        self._rec_bytes = 0
        self._post_remaining = None
        self._post_written = 0

        if allow_prerec and self._active_alert.pre_bytes > 0 and self._pre_buf_bytes > 0:
            pre_audio = b"".join(self._pre_buf)
            if pre_audio:
                take = min(self._pre_buf_bytes, self._active_alert.pre_bytes)
                self._wav.writeframes(pre_audio[-take:])
                pre_seconds_actual = take / self._bytes_per_second
                self._log(f"[same] Prepend: {take} bytes (~{pre_seconds_actual:.2f}s)")
        self._pre_buf.clear()
        self._pre_buf_bytes = 0

    def _start_reconstructed_record(self, header_line: str) -> None:
        self._active_alert = self.snapshot_alert_settings()
        self._wav = self._open_output_wav(header_line, self._active_alert)
        self._recording = True
        self._rec_bytes = 0
        self._post_remaining = None
        self._post_written = 0
        self._capture_skip_bytes = self._capture_delay_bytes
        self._capture_live_audio = False
        self._record_buf = bytearray()
        self._decode_pace_start = time.monotonic()
        self._decode_pace_bytes = 0
        self._wav.writeframes(self._reconstructed_prefix_bytes(header_line, self._active_alert))
        self._log(f"[same] Reconstructing SAME header, live capture begins in {self._capture_delay_seconds:.0f}s")
        self._pre_buf.clear()
        self._pre_buf_bytes = 0

    def _stop_record(self, reason: str, saw_eom: bool = False) -> None:
        alert = self._active_alert
        if alert is not None and alert.reconstruct_same and self._wav is not None:
            trim_bytes = 0
            if saw_eom and self._record_buf:
                trim_bytes = min(len(self._record_buf), self._trim_tail_bytes)
            live_audio = bytes(self._record_buf[:-trim_bytes] if trim_bytes else self._record_buf)
            try:
                if live_audio:
                    self._wav.writeframes(live_audio)
                self._wav.writeframes(self._reconstructed_suffix_bytes())
                self._wav.close()
            except Exception:
                try:
                    self._wav.close()
                except Exception:
                    pass
            self._wav = None
            self._recording = False
            self._post_remaining = None
            self._capture_skip_bytes = 0
            self._capture_live_audio = False
            self._record_buf = bytearray()
            self._decode_pace_start = None
            self._decode_pace_bytes = 0
            self._log(f"[same] STOP: {reason}")
            if trim_bytes > 0:
                trim_seconds_actual = trim_bytes / self._bytes_per_second
                self._log(f"[same] Trimmed: {trim_bytes} bytes (~{trim_seconds_actual:.2f}s)")
            self._maybe_convert_to_mp3(alert)
            self._active_alert = None
            return

        if self._wav is not None:
            try:
                self._wav.close()
            except Exception:
                pass
        self._wav = None
        self._recording = False
        self._post_remaining = None
        self._log(f"[same] STOP: {reason}")
        if self._post_written > 0:
            post_seconds_actual = self._post_written / self._bytes_per_second
            self._log(f"[same] Append: {self._post_written} bytes (~{post_seconds_actual:.2f}s)")
        self._post_written = 0
        if alert is not None:
            self._maybe_convert_to_mp3(alert)
        self._active_alert = None

    def _maybe_convert_to_mp3(self, alert: _AlertSettings) -> None:
        if alert.save_format != "mp3" or not self._cur_path:
            return
        wav_path = self._cur_path
        self._cur_path = None
        t_mp3 = threading.Thread(target=self._convert_to_mp3, args=(wav_path,), daemon=True)
        self._mp3_threads.append(t_mp3)
        t_mp3.start()

    def _convert_to_mp3(self, wav_path: str) -> None:
        mp3_path = os.path.splitext(wav_path)[0] + ".mp3"
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            self._log("[same] MP3 requested but ffmpeg is not available.")
            return
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            wav_path,
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "192k",
            mp3_path,
        ]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
                self._log(f"[same] Writing: {mp3_path}")
            else:
                self._log("[same] MP3 conversion failed; keeping WAV.")
        except Exception:
            self._log("[same] MP3 conversion failed; keeping WAV.")

    def _silence_bytes(self, seconds: float) -> bytes:
        return b"\x00\x00" * max(0, int(round(seconds * self.settings.rate)))

    def _tone_bytes(self, freqs, seconds: float, amplitude: float = 0.8) -> bytes:
        sample_count = max(0, int(round(seconds * self.settings.rate)))
        out = bytearray()
        scale = 32767.0 * amplitude / max(1, len(freqs))
        phases = [0.0 for _ in freqs]
        steps = [(2.0 * math.pi * freq) / self.settings.rate for freq in freqs]
        for _ in range(sample_count):
            sample = 0.0
            for idx, step in enumerate(steps):
                sample += math.sin(phases[idx])
                phases[idx] += step
                if phases[idx] >= 2.0 * math.pi:
                    phases[idx] -= 2.0 * math.pi
            value = max(-32767, min(32767, int(sample * scale)))
            out.extend(value.to_bytes(2, "little", signed=True))
        return bytes(out)

    def _same_burst_bytes(self, payload: str) -> bytes:
        out = bytearray()
        phase = 0.0
        mark_step = (2.0 * math.pi * 2083.3) / self.settings.rate
        space_step = (2.0 * math.pi * 1562.5) / self.settings.rate
        samples_per_bit = self.settings.rate / 520.83
        sample_cursor = 0
        sample_target = 0.0
        for byte in ([0xAB] * 16) + [ord(ch) & 0x7F for ch in payload]:
            for bit_idx in range(8):
                sample_target += samples_per_bit
                bit_samples = int(round(sample_target)) - sample_cursor
                sample_cursor += bit_samples
                step = mark_step if ((byte >> bit_idx) & 1) else space_step
                for _ in range(bit_samples):
                    value = int(24000 * math.sin(phase))
                    out.extend(value.to_bytes(2, "little", signed=True))
                    phase += step
                    if phase >= 2.0 * math.pi:
                        phase -= 2.0 * math.pi
        return bytes(out)

    def _reconstructed_prefix_bytes(self, header_line: str, alert: _AlertSettings) -> bytes:
        gap = self._silence_bytes(1.0)
        burst = self._same_burst_bytes(header_line)
        out = bytearray()
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        if alert.tone == "ebs":
            out.extend(self._tone_bytes([853.0, 960.0], alert.tone_duration, amplitude=0.7))
            out.extend(gap)
        elif alert.tone == "nwr":
            out.extend(self._tone_bytes([1050.0], alert.tone_duration, amplitude=0.8))
            out.extend(gap)
        return bytes(out)

    def _reconstructed_suffix_bytes(self) -> bytes:
        gap = self._silence_bytes(1.0)
        burst = self._same_burst_bytes("NNNN")
        out = bytearray()
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        return bytes(out)

    def _validate_static_settings(self) -> None:
        if self.settings.rate <= 0:
            raise ValueError("rate must be greater than zero")
        if self.settings.detect_rate <= 0:
            raise ValueError("detect_rate must be greater than zero")
        self._validate_alert_settings()

    def _validate_alert_settings(self) -> None:
        settings = self.settings
        now_for_year = datetime.now() if settings.local_time else datetime.now(timezone.utc)
        current_year = now_for_year.year
        if settings.year is not None and (settings.year < 1997 or settings.year > current_year):
            raise ValueError(f"year must be between 1997 and {current_year}")
        if settings.reconstruct_same and (settings.pre_seconds != 0.0 or settings.post_seconds != 0.0):
            raise ValueError("pre_seconds and post_seconds cannot be used with reconstruct_same")
        if not settings.reconstruct_same and (settings.tone != "none" or settings.tone_duration is not None):
            raise ValueError("tone and tone_duration require reconstruct_same")
        if settings.tone == "none" and settings.tone_duration is not None:
            raise ValueError("tone_duration requires tone='ebs' or tone='nwr'")
        tone_duration = 10.0 if settings.tone_duration is None else settings.tone_duration
        if settings.tone != "none" and not (8.0 <= tone_duration <= 25.0):
            raise ValueError("tone_duration must be between 8 and 25 seconds")
        if settings.save_format not in SAVE_FORMATS:
            raise ValueError("save_format must be 'wav' or 'mp3'")

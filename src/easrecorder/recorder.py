from __future__ import annotations

import math
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Callable, TextIO

import numpy as np

from .attention_tone_detector import ATTENTION_TONE_MIN_SECONDS, AttentionTone, AttentionToneTracker
from .mp3_encoder import Mp3Encoder
from .same_preamble_detector import SAME_BAUD, SameBurst, SamePreambleTracker
from .soxr_native import SoxrStream


MAX_PRERECORD_SECONDS = 10.0
MAX_POSTRECORD_SECONDS = 10.0
SAVE_FORMATS = {"wav", "mp3"}
DECODER_DRAIN_TIMEOUT_SECONDS = 1.0
MP3_SAMPLE_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)
MP3_BITRATE_KBPS = 192
INDEX_VERSION = 1
# How far back the first of the three header bursts can be when the decoder confirms
# the header (normally after the second burst; after the third if one was garbled).
HEADER_LOOKBACK_SECONDS = 20.0
# Recordings start this far before the detected start of the first header burst.
HEADER_START_MARGIN_SECONDS = 0.05
# Header bursts are about one second apart; allow extra before treating an earlier
# preamble as unrelated.
HEADER_BURST_GAP_SECONDS = 2.5
# Partial decodes arrive a little after their burst ends; back up this much more.
PARTIAL_DECODE_MARGIN_SECONDS = 0.5
# EOM bursts last about 0.3 s and the shortest possible header about 0.9 s.
HEADER_MIN_BURST_SECONDS = 0.6
# Alert audio reaches the file this long after it arrives, so that when a new header
# is confirmed the file can still end where that header's first burst began.
WRITE_DELAY_SECONDS = HEADER_LOOKBACK_SECONDS
# An attention tone normally starts 2-4 s after the third header burst; one starting
# later than this isn't treated as part of the alert.
ATTENTION_TONE_WINDOW_SECONDS = 6.0
# EAS attention tones last 8-25 s. If one runs longer, start max_seconds anyway.
ATTENTION_TONE_MAX_SECONDS = 30.0
ATTENTION_TONE_NAMES = {"nwr": "1050 Hz", "ebs": "853 + 960 Hz"}
EOM_BURSTS = 3
# If fewer than three EOMs decode, end at the last one once no other arrives in time.
EOM_WAIT_SECONDS = 4.0
SAME_HEADER_RE = re.compile(
    r"^ZCZC-(?P<originator>[A-Z0-9]{3})-(?P<event_type>[A-Z0-9]{3})-"
    r"(?P<fips_codes>\d{6}(?:-\d{6})*)\+(?P<duration_code>\d{4})-"
    r"(?P<timestamp>\d{7})-(?P<sender_id>[^-]{1,8})-?$"
)


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
    index_path: str | None = None


@dataclass(frozen=True)
class SameHeader:
    raw_header: str
    event_type: str
    originator: str
    fips_codes: tuple[str, ...]
    start_time_utc: datetime
    duration_code: str
    duration_seconds: int
    sender_id: str


@dataclass
class _HeaderCluster:
    """Partial decodes of one header, close enough together to be the same alert."""

    header: str
    positions: list[int] = field(default_factory=list)
    triggered: bool = False


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
    index_path: str | None


def parse_same_header(
    header_line: str,
    year: int | None = None,
    now: datetime | None = None,
) -> SameHeader:
    """Parse a SAME header while preserving unknown event/originator codes."""

    raw_header = header_line.strip()
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)
    header_year = year if year is not None else now_utc.year
    match = SAME_HEADER_RE.fullmatch(raw_header)
    if match is None:
        parts = raw_header.split("-")
        originator = parts[1] if len(parts) > 1 and parts[1] else "UNK"
        event_type = parts[2] if len(parts) > 2 and parts[2] else "UNK"
        return SameHeader(
            raw_header=raw_header,
            event_type=event_type,
            originator=originator,
            fips_codes=(),
            start_time_utc=now_utc.replace(second=0, microsecond=0),
            duration_code="0000",
            duration_seconds=0,
            sender_id="UNKNOWN",
        )

    duration_code = match.group("duration_code")
    duration_seconds = (int(duration_code[:2]) * 60 + int(duration_code[2:])) * 60
    timestamp = match.group("timestamp")
    try:
        julian_day = int(timestamp[:3])
        hour = int(timestamp[3:5])
        minute = int(timestamp[5:7])
        if not 1 <= julian_day <= 366 or hour > 23 or minute > 59:
            raise ValueError
        start_time_utc = datetime(header_year, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=julian_day - 1,
            hours=hour,
            minutes=minute,
        )
        if start_time_utc.year != header_year:
            raise ValueError
    except ValueError:
        start_time_utc = now_utc.replace(second=0, microsecond=0)

    return SameHeader(
        raw_header=raw_header,
        event_type=match.group("event_type"),
        originator=match.group("originator"),
        fips_codes=tuple(match.group("fips_codes").split("-")),
        start_time_utc=start_time_utc,
        duration_code=duration_code,
        duration_seconds=duration_seconds,
        sender_id=match.group("sender_id").strip() or "UNKNOWN",
    )


def _reader_thread(proc: subprocess.Popen, q: deque[str]) -> None:
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        q.append(line.decode("utf-8", errors="ignore").rstrip("\n"))


def validate_cli_settings(settings: RecorderSettings) -> None:
    """Validate combinations that are invalid for one-shot CLI startup."""

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


class EASRecorder:
    """Record EAS alerts from raw signed 16-bit little-endian mono PCM.

    Use ``run()`` to read from a stream until EOF, or use ``start()``,
    ``write()``, and ``stop()`` when another Python component produces samples.
    Alert-scoped settings are snapshotted when a new SAME header starts.

    ``on_alert``, if set, is called once for each alert after its recording is
    saved on disk, with a dict holding the same fields as an alert index entry.
    It may be called from a background thread when saving MP3 files.
    """

    def __init__(
        self,
        settings: RecorderSettings,
        input_stream: BinaryIO | None = None,
        output_stream: BinaryIO | None = None,
        log_stream: TextIO | None = None,
        on_alert: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.on_alert = on_alert
        self.input_stream = input_stream if input_stream is not None else sys.stdin.buffer
        self.output_stream = output_stream if output_stream is not None else sys.stdout.buffer
        self.log_stream = log_stream if log_stream is not None else (
            sys.stderr if settings.copy_stdout else sys.stdout
        )

        self._mm: subprocess.Popen | None = None
        self._detector_resampler: SoxrStream | None = None
        self._pcm_remainder = b""
        self._lines: deque[str] = deque()
        self._reader: threading.Thread | None = None
        self._started = False

        self._recording = False
        self._wav = None
        self._cur_path: str | None = None
        self._post_written = 0
        self._active_header: SameHeader | None = None
        self._decode_pace_start: float | None = None
        self._decode_pace_bytes = 0
        self._mp3_threads: list[threading.Thread] = []
        self._index_lock = threading.Lock()
        self._active_alert: _AlertSettings | None = None
        self._claimed_paths: set[str] = set()

        # Stream positions below are byte offsets from the first sample after start().
        # _history keeps recent audio whether or not an alert is recording; the file
        # holds the alert from its start up to _committed.
        self._history = bytearray()
        self._history_start = 0
        self._routed_bytes = 0
        self._detected_bytes = 0
        self._preamble_tracker = SamePreambleTracker(settings.rate)
        self._bursts: deque[SameBurst] = deque()
        self._tone_tracker = AttentionToneTracker(settings.rate)
        self._tones: deque[AttentionTone] = deque()
        self._clusters: deque[_HeaderCluster] = deque()
        self._committed = 0
        self._last_cut = 0
        self._alert_floor = 0
        self._eom_count = 0
        self._eoms_done = False
        self._eom_reason = "EOM"
        self._last_eom_pos: int | None = None
        # Where the active alert's header was decoded and its first burst began, and
        # the attention tone that followed it, once known (_tone_resolved).
        self._trigger_pos = 0
        self._header_start = 0
        self._tone_deadline = 0
        self._tone_resolved = False
        self._tone_end: int | None = None
        # Reconstruction mode: where live audio starts, once known.
        self._capture_start: int | None = None

        self._bytes_per_second = settings.rate * 2
        self._history_max_bytes = int(
            (MAX_PRERECORD_SECONDS + HEADER_LOOKBACK_SECONDS) * self._bytes_per_second
        )
        self._eom_wait_bytes = int(EOM_WAIT_SECONDS * self._bytes_per_second)
        self._write_delay_bytes = self._seconds_to_bytes(WRITE_DELAY_SECONDS)
        self._header_margin_bytes = self._seconds_to_bytes(HEADER_START_MARGIN_SECONDS)
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

        self._mm = subprocess.Popen(
            # -v 1 also prints each header burst as it decodes ("EAS (part): ..."),
            # before two bursts agree and the confirmed "EAS: ..." line is printed.
            ["multimon-ng", "-v", "1", "-t", "raw", "-a", "EAS", "-f", str(self.settings.detect_rate), "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if self.settings.rate == self.settings.detect_rate:
            self._detector_resampler = None
        else:
            self._detector_resampler = SoxrStream(self.settings.rate, self.settings.detect_rate, "int16")
        self._pcm_remainder = b""
        self._lines.clear()
        self._reset_stream()
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

        detector_audio = self._pcm_remainder + samples
        complete_bytes = len(detector_audio) - (len(detector_audio) % 2)
        audio = detector_audio[:complete_bytes]
        self._pcm_remainder = detector_audio[complete_bytes:]
        if not audio:
            return
        if self.settings.copy_stdout:
            try:
                self.output_stream.write(audio)
                flush = getattr(self.output_stream, "flush", None)
                if flush is not None:
                    flush()
            except BrokenPipeError as exc:
                raise RuntimeError("stdout pipeline exited") from exc

        try:
            assert self._mm is not None
            assert self._mm.stdin is not None
            self._mm.stdin.write(self._resample_for_detector(audio))
        except BrokenPipeError as exc:
            raise RuntimeError("multimon-ng pipeline exited") from exc

        if self._active_alert is not None and self._active_alert.reconstruct_same and self._recording:
            self._pace_reconstruction_decode(len(audio))

        self._detect_preambles(audio)
        self._detect_attention_tones(audio)
        self._process_decoded_lines()
        self._write_alert_audio(audio)

    def stop(self, reason: str = "shutdown") -> None:
        """Stop the recorder and close any active alert."""

        if self._started:
            self._drain_decoder()
        if self._recording:
            try:
                end = self._routed_bytes
                if self._active_alert is not None and self._last_eom_pos is not None:
                    end = min(end, self._last_eom_pos + self._active_alert.post_bytes)
                self._stop_at(end, reason)
            except Exception:
                pass
        try:
            if self._mm is not None and self._mm.stdin is not None:
                self._mm.stdin.close()
        except Exception:
            pass
        for proc in (self._mm,):
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
        self._mp3_threads.clear()
        self._lines.clear()
        self._reset_stream()
        self._started = False
        self._mm = None
        self._detector_resampler = None
        self._pcm_remainder = b""
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
            index_path=settings.index_path,
        )

    def _log(self, msg: str) -> None:
        print(msg, file=self.log_stream, flush=True)

    def _drain_decoder(self) -> None:
        try:
            if self._mm is not None and self._mm.stdin is not None:
                if self._detector_resampler is not None:
                    tail = self._detector_resampler.process(np.empty(0, dtype=np.int16), last=True)
                    if tail.size:
                        self._mm.stdin.write(tail.tobytes())
                self._mm.stdin.close()
        except Exception:
            pass
        try:
            if self._mm is not None:
                self._mm.wait(timeout=DECODER_DRAIN_TIMEOUT_SECONDS)
        except Exception:
            pass
        if self._reader is not None:
            self._reader.join(timeout=DECODER_DRAIN_TIMEOUT_SECONDS)
        self._process_decoded_lines()

    def _resample_for_detector(self, audio: bytes) -> bytes:
        if self._detector_resampler is None:
            return audio
        samples = np.frombuffer(audio, dtype="<i2").astype(np.int16, copy=False)
        return self._detector_resampler.process(samples).tobytes()

    def _process_decoded_lines(self) -> None:
        while self._lines:
            line = self._lines.popleft()
            if "EAS (part):" in line:
                payload = line.split("EAS (part):", 1)[1].strip()
                if payload.startswith("ZCZC"):
                    self._handle_partial_header(payload)
                continue
            if "EAS:" not in line:
                continue
            payload = line.split("EAS:", 1)[1].strip()

            if payload.startswith("ZCZC"):
                self._handle_confirmed_header(payload)

            if payload.startswith("NNNN") and self._recording:
                if self._active_alert is not None and self._active_alert.reconstruct_same:
                    # Generated EOMs replace the received ones, so live audio ends
                    # where the first EOM began.
                    self._stop_at(self._eom_burst_start(), "EOM")
                elif not self._eoms_done:
                    self._handle_eom()

    def _handle_partial_header(self, header_line: str) -> None:
        # multimon-ng prints a confirmed header only once two bursts match, and never
        # prints one identical to the last header it confirmed, even for a later alert.
        # Matching the partial decodes here catches repeated alerts too.
        position = self._routed_bytes
        cluster = self._latest_cluster(header_line)
        if (
            cluster is None
            or len(cluster.positions) >= 3
            or position - cluster.positions[-1] > self._cluster_window_bytes(header_line)
        ):
            cluster = _HeaderCluster(header_line)
            self._clusters.append(cluster)
        cluster.positions.append(position)
        if len(cluster.positions) >= 2 and not cluster.triggered:
            cluster.triggered = True
            self._begin_alert(header_line)

    def _handle_confirmed_header(self, header_line: str) -> None:
        cluster = self._latest_cluster(header_line)
        if cluster is not None and (
            self._routed_bytes - cluster.positions[-1] <= self._cluster_window_bytes(header_line)
        ):
            if cluster.triggered:
                return
            cluster.triggered = True
        self._begin_alert(header_line)

    def _begin_alert(self, header_line: str) -> None:
        if self._recording:
            self._end_for_new_header(header_line)
        if self.settings.reconstruct_same:
            self._start_reconstructed_record(header_line)
        else:
            self._start_record(header_line)

    def _end_for_new_header(self, header_line: str) -> None:
        """End the active alert where the new header's first burst began."""

        alert = self._active_alert
        first_burst = self._first_header_burst(header_line)
        cut = self._routed_bytes if first_burst is None else first_burst[0] - self._header_margin_bytes
        if self._last_eom_pos is not None:
            cut = min(cut, self._last_eom_pos + alert.post_bytes)
            reason = f"{self._eom_reason}, next alert followed"
        else:
            reason = "interrupted by a new header"
        self._stop_at(cut, reason)

    def _handle_eom(self) -> None:
        self._eom_count += 1
        self._last_eom_pos = self._routed_bytes
        self._log(f"[same] EOM {self._eom_count} of {EOM_BURSTS}")
        if self._eom_count >= EOM_BURSTS:
            self._eoms_done = True
            self._eom_reason = "EOM"
        self._check_alert_end()

    def _check_alert_end(self) -> None:
        alert = self._active_alert
        if alert is None:
            return
        timer_start = self._alert_timer_start()
        if alert.reconstruct_same and self._capture_start is None:
            if timer_start is None:
                return
            self._begin_capture(timer_start)
        if alert.max_bytes > 0 and timer_start is not None:
            if self._routed_bytes >= timer_start + alert.max_bytes:
                self._stop_at(timer_start + alert.max_bytes, "timeout")
                return
        if self._last_eom_pos is None:
            return
        if not self._eoms_done:
            # Fewer than three EOMs decoded; give up waiting and end at the last one.
            if self._routed_bytes - self._last_eom_pos < self._eom_wait_bytes:
                return
            self._eoms_done = True
            self._eom_reason = f"EOM ({self._eom_count} of {EOM_BURSTS} heard)"
        end = self._last_eom_pos + alert.post_bytes
        next_header = self._next_header_burst(self._last_eom_pos)
        if next_header is not None and next_header < end:
            self._stop_at(next_header, f"{self._eom_reason}, next alert followed")
        elif self._routed_bytes >= end:
            self._stop_at(end, self._eom_reason)

    def _stop_at(self, end: int, reason: str) -> None:
        """Write the alert up to stream position ``end`` and close it."""

        alert = self._active_alert
        if alert is not None and alert.reconstruct_same and self._capture_start is None:
            # Ending before live capture was due to start; capture whatever is left.
            self._begin_capture(min(end, self._alert_timer_start(force=True)))
        end = max(self._committed, min(end, self._routed_bytes))
        end -= end % 2
        self._commit(end)
        if self._last_eom_pos is not None and end > self._last_eom_pos:
            self._post_written = end - self._last_eom_pos
        self._last_cut = end
        self._stop_record(reason)

    def _commit(self, upto: int) -> None:
        upto = min(upto, self._routed_bytes)
        if self._wav is None or upto <= self._committed:
            return
        begin = max(self._committed, self._history_start)
        self._wav.writeframes(
            bytes(self._history[begin - self._history_start : upto - self._history_start])
        )
        self._committed = upto

    def _detect_preambles(self, audio: bytes) -> None:
        self._bursts.extend(self._preamble_tracker.feed(audio))
        self._detected_bytes += len(audio)

    def _detect_attention_tones(self, audio: bytes) -> None:
        self._tones.extend(self._tone_tracker.feed(audio))

    def _alert_timer_start(self, force: bool = False) -> int | None:
        """Where max_seconds starts counting, or None while that isn't known yet.

        That is the end of the attention tone if one follows the header. Otherwise it
        is the decoded header, or in reconstruction mode, the end of the third header
        burst, where live audio starts.
        """

        if not self._resolve_attention_tone(force):
            return None
        if self._tone_end is not None:
            return self._tone_end
        if self._active_alert is not None and self._active_alert.reconstruct_same:
            return self._third_header_burst_end()
        return self._trigger_pos

    def _resolve_attention_tone(self, force: bool = False) -> bool:
        """Decide whether an attention tone follows the header; True once decided.

        With ``force``, decide now with what has been heard so far.
        """

        if self._tone_resolved:
            return True
        min_tone_bytes = self._seconds_to_bytes(ATTENTION_TONE_MIN_SECONDS)
        max_tone_bytes = self._seconds_to_bytes(ATTENTION_TONE_MAX_SECONDS)
        tone = next(
            (
                tone
                for tone in self._tones
                if self._header_start <= tone.start * 2 <= self._tone_deadline
            ),
            None,
        )
        if tone is None:
            # A tone is only reported once it has lasted the minimum, so wait that
            # long past the deadline before deciding there is none.
            if not force and self._detected_bytes < self._tone_deadline + min_tone_bytes:
                return False
            self._log("[same] No attention tone after the header")
            self._tone_resolved = True
            return True
        start = tone.start * 2
        if tone.end is not None:
            end = tone.end * 2
        elif self._detected_bytes - start >= max_tone_bytes:
            end = start + max_tone_bytes
        elif force:
            end = self._detected_bytes
        else:
            return False
        self._log(
            f"[same] Attention tone {ATTENTION_TONE_NAMES[tone.kind]} lasted "
            f"{(end - start) / self._bytes_per_second:.1f}s"
        )
        self._tone_resolved = True
        self._tone_end = end
        return True

    def _third_header_burst_end(self) -> int:
        header_line = self._active_header.raw_header if self._active_header is not None else ""
        burst_bytes = self._burst_bytes(header_line)
        latest_start = self._trigger_pos + burst_bytes + self._seconds_to_bytes(HEADER_BURST_GAP_SECONDS)
        min_bytes = self._seconds_to_bytes(HEADER_MIN_BURST_SECONDS)
        ends = []
        for burst in self._bursts:
            start, end = self._burst_span(burst)
            if self._header_start <= start <= latest_start and end is not None and end - start >= min_bytes:
                ends.append(end)
        if ends:
            return max(ends)
        # Not detected: the header is normally decoded right after its second burst,
        # and the third follows after a one second gap.
        return self._trigger_pos + burst_bytes + self._seconds_to_bytes(1.0)

    def _eom_burst_start(self) -> int:
        """Cut position for the EOM burst that was just decoded."""

        floor = self._capture_start if self._capture_start is not None else self._header_start
        eom_bytes = self._burst_bytes("NNNN")
        recent = self._routed_bytes - eom_bytes - self._seconds_to_bytes(2.0)
        for burst in reversed(self._bursts):
            start, _ = self._burst_span(burst)
            if start < max(floor, recent):
                break
            if start <= self._routed_bytes:
                return start - self._header_margin_bytes
        return self._routed_bytes - eom_bytes - self._seconds_to_bytes(PARTIAL_DECODE_MARGIN_SECONDS)

    def _begin_capture(self, start: int) -> None:
        start = max(start, self._history_start)
        start -= start % 2
        self._capture_start = start
        self._committed = start
        self._log("[same] Capturing alert audio")

    def _begin_alert_timing(self, header_line: str, header_start: int) -> None:
        self._trigger_pos = self._routed_bytes
        self._header_start = header_start
        self._tone_resolved = False
        self._tone_end = None
        self._capture_start = None
        # The third burst ends about one burst and gap after the header is confirmed.
        self._tone_deadline = (
            self._routed_bytes
            + self._burst_bytes(header_line)
            + self._seconds_to_bytes(1.0 + ATTENTION_TONE_WINDOW_SECONDS)
        )
        # This alert's own later header bursts must not be taken for a new alert.
        self._alert_floor = (
            self._routed_bytes
            + self._burst_bytes(header_line)
            + self._seconds_to_bytes(HEADER_BURST_GAP_SECONDS)
        )

    def _append_history(self, audio: bytes) -> None:
        self._history.extend(audio)
        excess = len(self._history) - self._history_max_bytes
        # Trim in steps of about a second rather than on every chunk.
        if excess >= self._bytes_per_second:
            excess -= excess % 2
            del self._history[:excess]
            self._history_start += excess
            while self._bursts and self._bursts[0].start * 2 < self._history_start:
                self._bursts.popleft()
            while self._tones and self._tones[0].end is not None and self._tones[0].end * 2 < self._history_start:
                self._tones.popleft()
            while self._clusters and self._clusters[0].positions[-1] < self._history_start:
                self._clusters.popleft()

    def _reset_stream(self) -> None:
        self._history = bytearray()
        self._history_start = 0
        self._routed_bytes = 0
        self._detected_bytes = 0
        self._preamble_tracker = SamePreambleTracker(self.settings.rate)
        self._bursts.clear()
        self._tone_tracker = AttentionToneTracker(self.settings.rate)
        self._tones.clear()
        self._clusters.clear()
        self._committed = 0
        self._last_cut = 0
        self._alert_floor = 0

    def _seconds_to_bytes(self, seconds: float) -> int:
        return int(round(seconds * self.settings.rate)) * 2

    def _burst_bytes(self, header_line: str) -> int:
        return self._seconds_to_bytes((16 + len(header_line)) * 8 / SAME_BAUD)

    def _cluster_window_bytes(self, header_line: str) -> int:
        # Two bursts apart, in case the burst between them did not decode.
        return 2 * (self._burst_bytes(header_line) + self._seconds_to_bytes(HEADER_BURST_GAP_SECONDS))

    def _latest_cluster(self, header_line: str) -> _HeaderCluster | None:
        for cluster in reversed(self._clusters):
            if cluster.header == header_line:
                return cluster
        return None

    def _burst_span(self, burst: SameBurst) -> tuple[int, int | None]:
        return burst.start * 2, None if burst.end is None else burst.end * 2

    def _header_floor(self) -> int:
        """Earliest position a newly confirmed header's first burst can be at."""

        return max(self._history_start, self._last_cut, self._alert_floor)

    def _next_header_burst(self, after: int) -> int | None:
        """Cut position for the first header-length SAME burst starting at or after ``after``."""

        min_bytes = self._seconds_to_bytes(HEADER_MIN_BURST_SECONDS)
        for burst in self._bursts:
            start, end = self._burst_span(burst)
            length = (end if end is not None else self._detected_bytes) - start
            if start >= after and length >= min_bytes:
                return max(after, start - self._header_margin_bytes)
        return None

    def _first_header_burst(self, header_line: str) -> tuple[int, str] | None:
        """Stream position where the first burst of this header began, if it is known."""

        floor = self._header_floor()
        burst_bytes = self._burst_bytes(header_line)
        max_gap = burst_bytes + self._seconds_to_bytes(HEADER_BURST_GAP_SECONDS)
        min_bytes = self._seconds_to_bytes(HEADER_MIN_BURST_SECONDS)

        starts = []
        for burst in self._bursts:
            start, end = self._burst_span(burst)
            # Skip bursts known to be too short for a header, such as EOMs.
            if start >= floor and (end is None or end - start >= min_bytes):
                starts.append(start)
        # The newest burst should be the one that was just decoded.
        if starts and self._routed_bytes - starts[-1] <= max_gap:
            first = starts[-1]
            count = 1
            for start in reversed(starts[:-1]):
                if count >= 3 or first - start > max_gap:
                    break
                first = start
                count += 1
            return first, "preamble detector"

        cluster = self._latest_cluster(header_line)
        if cluster is not None and self._routed_bytes - cluster.positions[-1] <= self._cluster_window_bytes(header_line):
            margin = self._seconds_to_bytes(PARTIAL_DECODE_MARGIN_SECONDS)
            return max(floor, cluster.positions[0] - burst_bytes - margin), "first decoded burst"
        return None

    def _write_alert_audio(self, audio: bytes) -> None:
        self._routed_bytes += len(audio)
        self._append_history(audio)
        if not self._recording or self._wav is None or self._active_alert is None:
            return
        self._check_alert_end()
        if not self._recording:
            return
        if self._active_alert.reconstruct_same and self._capture_start is None:
            return
        self._commit(self._routed_bytes - self._write_delay_bytes)

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
        parsed = parse_same_header(header_line, year=alert.name_year)
        timestamp = parsed.start_time_utc.astimezone() if alert.local_time else parsed.start_time_utc
        return (
            parsed.event_type,
            timestamp.strftime("%m-%d-%Y"),
            timestamp.strftime("%H%M"),
            timestamp.tzname() or ("LOCAL" if alert.local_time else "UTC"),
        )

    def _build_output_path(self, header_line: str, alert: _AlertSettings) -> str:
        event, date_str, time_str, tz_str = self._parse_event_and_timestamp(header_line, alert)
        if alert.prefix:
            base = f"{alert.prefix}-{event}-{date_str}-{time_str}{tz_str}"
        else:
            base = f"{event}-{date_str}-{time_str}{tz_str}"
        return os.path.join(alert.outdir, f"{base}.wav")

    def _unique_output_path(self, path: str) -> str:
        # Back-to-back alerts can share a name (same event and minute, or a repeated
        # header), so never reuse a file this run wrote or one already on disk.
        base, ext = os.path.splitext(path)
        candidate = path
        number = 2
        while (
            candidate in self._claimed_paths
            or os.path.exists(candidate)
            or os.path.exists(os.path.splitext(candidate)[0] + ".mp3")
        ):
            candidate = f"{base}-{number}{ext}"
            number += 1
        self._claimed_paths.add(candidate)
        return candidate

    def _open_output_wav(self, header_line: str, alert: _AlertSettings):
        os.makedirs(alert.outdir, exist_ok=True)
        path = self._unique_output_path(self._build_output_path(header_line, alert))
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

    def _start_record(self, header_line: str) -> None:
        self._active_alert = self.snapshot_alert_settings()
        self._active_header = parse_same_header(header_line, year=self._active_alert.name_year)
        self._wav = self._open_output_wav(header_line, self._active_alert)
        self._recording = True
        self._post_written = 0
        self._eom_count = 0
        self._eoms_done = False
        self._last_eom_pos = None

        # The header is confirmed only after its second burst, so start where the
        # first burst began, then pre_seconds further, but never before the end of
        # the previous recording.
        start = self._routed_bytes
        first_burst = self._first_header_burst(header_line)
        if first_burst is not None:
            start = first_burst[0] - self._header_margin_bytes
            self._log(f"[same] First header burst found by {first_burst[1]}")
        else:
            self._log("[same] First header burst not found; starting at the decoded header")
        header_start = start
        start = max(start - self._active_alert.pre_bytes, self._header_floor())
        start -= start % 2
        self._committed = start
        self._begin_alert_timing(header_line, header_start)
        prepend = self._routed_bytes - start
        if prepend > 0:
            self._log(f"[same] Prepend: {prepend} bytes (~{prepend / self._bytes_per_second:.2f}s)")

    def _start_reconstructed_record(self, header_line: str) -> None:
        self._active_alert = self.snapshot_alert_settings()
        self._active_header = parse_same_header(header_line, year=self._active_alert.name_year)
        self._wav = self._open_output_wav(header_line, self._active_alert)
        self._recording = True
        self._post_written = 0
        self._decode_pace_start = time.monotonic()
        self._decode_pace_bytes = 0
        first_burst = self._first_header_burst(header_line)
        header_start = self._routed_bytes if first_burst is None else first_burst[0]
        self._begin_alert_timing(header_line, header_start)
        self._wav.writeframes(self._reconstructed_prefix_bytes(header_line, self._active_alert))
        self._log("[same] Reconstructing SAME header; live audio starts after the attention tone or third header")

    def _stop_record(self, reason: str) -> None:
        alert = self._active_alert
        header = self._active_header
        if alert is not None and alert.reconstruct_same and self._wav is not None:
            try:
                self._wav.writeframes(self._reconstructed_suffix_bytes())
                self._wav.close()
            except Exception:
                try:
                    self._wav.close()
                except Exception:
                    pass
            self._wav = None
            self._recording = False
            self._alert_floor = 0
            self._capture_start = None
            self._decode_pace_start = None
            self._decode_pace_bytes = 0
            self._log(f"[same] STOP: {reason}")
            self._finalize_output(alert, header)
            self._active_alert = None
            self._active_header = None
            return

        if self._wav is not None:
            try:
                self._wav.close()
            except Exception:
                pass
        self._wav = None
        self._recording = False
        self._eom_count = 0
        self._eoms_done = False
        self._last_eom_pos = None
        self._alert_floor = 0
        self._log(f"[same] STOP: {reason}")
        if self._post_written > 0:
            post_seconds_actual = self._post_written / self._bytes_per_second
            self._log(f"[same] Append: {self._post_written} bytes (~{post_seconds_actual:.2f}s)")
        self._post_written = 0
        if alert is not None:
            self._finalize_output(alert, header)
        self._active_alert = None
        self._active_header = None

    def _finalize_output(self, alert: _AlertSettings, header: SameHeader | None) -> None:
        if not self._cur_path:
            return
        wav_path = self._cur_path
        self._cur_path = None
        if alert.save_format != "mp3":
            self._set_output_timestamp(wav_path, header)
            self._alert_saved(alert, header, wav_path)
            return
        t_mp3 = threading.Thread(
            target=self._convert_to_mp3,
            args=(wav_path, alert, header),
            daemon=True,
        )
        self._mp3_threads.append(t_mp3)
        t_mp3.start()

    def _convert_to_mp3(
        self,
        wav_path: str,
        alert: _AlertSettings | None = None,
        header: SameHeader | None = None,
    ) -> None:
        mp3_path = os.path.splitext(wav_path)[0] + ".mp3"
        temp_mp3_path = mp3_path + ".tmp"
        final_path = wav_path
        try:
            with wave.open(wav_path, "rb") as source:
                source_rate = source.getframerate()
                target_rate = min(MP3_SAMPLE_RATES, key=lambda rate: abs(rate - source_rate))
                resampler = None
                if source_rate != target_rate:
                    resampler = SoxrStream(source_rate, target_rate, "int16")

                with Mp3Encoder(target_rate, MP3_BITRATE_KBPS) as encoder, open(temp_mp3_path, "wb") as output:
                    while True:
                        pcm = source.readframes(8192)
                        if not pcm:
                            break
                        if resampler is not None:
                            samples = np.frombuffer(pcm, dtype="<i2").astype(np.int16, copy=False)
                            pcm = resampler.process(samples).tobytes()
                        if pcm:
                            output.write(encoder.encode(pcm))
                    if resampler is not None:
                        tail = resampler.process(np.empty(0, dtype=np.int16), last=True)
                        if tail.size:
                            output.write(encoder.encode(tail.tobytes()))
                    output.write(encoder.flush())

            os.replace(temp_mp3_path, mp3_path)
            final_path = mp3_path
            self._set_output_timestamp(mp3_path, header)
            os.remove(wav_path)
            self._log(f"[same] Writing: {mp3_path}")
        except Exception as exc:
            try:
                os.remove(temp_mp3_path)
            except OSError:
                pass
            self._set_output_timestamp(wav_path, header)
            self._log(f"[same] MP3 conversion failed ({exc}); keeping WAV.")
        if alert is not None:
            self._alert_saved(alert, header, final_path)

    def _set_output_timestamp(self, output_path: str, header: SameHeader | None) -> None:
        if header is None:
            return
        issued_at = header.start_time_utc.astimezone(timezone.utc).timestamp()
        try:
            os.utime(output_path, (issued_at, issued_at))
        except OSError as exc:
            self._log(f"[same] Timestamp update failed: {exc}")

    def _alert_saved(self, alert: _AlertSettings, header: SameHeader | None, output_path: str) -> None:
        """Index the saved recording and tell the application about it."""

        if header is None:
            return
        entry = self._alert_entry(header, output_path)
        if alert.index_path is not None:
            self._write_index_entry(alert, entry)
        callback = self.on_alert
        if callback is not None:
            try:
                callback(dict(entry, fips_codes=list(entry["fips_codes"])))
            except Exception as exc:
                self._log(f"[same] on_alert callback failed: {exc!r}")

    def _alert_entry(self, header: SameHeader, output_path: str) -> dict[str, Any]:
        start_time = header.start_time_utc.astimezone(timezone.utc)
        expires_at = start_time + timedelta(seconds=header.duration_seconds)
        return {
            "raw_same_header": header.raw_header,
            "event_type": header.event_type,
            "originator": header.originator,
            "fips_codes": list(header.fips_codes),
            "start_time_utc": start_time.isoformat().replace("+00:00", "Z"),
            "duration_code": header.duration_code,
            "duration_seconds": header.duration_seconds,
            "expires_at_utc": expires_at.isoformat().replace("+00:00", "Z"),
            "sender_id": header.sender_id,
            "file_path": os.path.abspath(output_path),
        }

    def _write_index_entry(self, alert: _AlertSettings, entry: dict[str, Any]) -> None:
        index_path = alert.index_path
        if index_path is None:
            return
        if not os.path.isabs(index_path):
            index_path = os.path.join(alert.outdir, index_path)
        index_path = os.path.abspath(index_path)

        with self._index_lock:
            try:
                data = self._read_index(index_path)
                alerts = data["alerts"]
                alerts.insert(0, entry)
                alerts.sort(key=lambda item: item.get("start_time_utc", ""), reverse=True)
                self._replace_index(index_path, data)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._log(f"[same] Index update failed: {exc}")

    def _read_index(self, index_path: str) -> dict:
        if not os.path.exists(index_path):
            return {"version": INDEX_VERSION, "alerts": []}
        with open(index_path, "r", encoding="utf-8") as source:
            data = json.load(source)
        if isinstance(data, list):
            alerts = data
            data = {"version": INDEX_VERSION, "alerts": alerts}
        if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
            raise ValueError("index must be an object containing an alerts array")
        if not all(isinstance(entry, dict) for entry in data["alerts"]):
            raise ValueError("every index alert must be a JSON object")
        data["version"] = INDEX_VERSION
        return data

    def _replace_index(self, index_path: str, data: dict) -> None:
        parent = os.path.dirname(index_path)
        os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(data, output, indent=2)
                output.write("\n")
            os.replace(temp_path, index_path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

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
        if self.settings.save_format not in SAVE_FORMATS:
            raise ValueError("save_format must be 'wav' or 'mp3'")

    def _validate_alert_settings(self) -> None:
        settings = self.settings
        now_for_year = datetime.now() if settings.local_time else datetime.now(timezone.utc)
        current_year = now_for_year.year
        if settings.year is not None and (settings.year < 1997 or settings.year > current_year):
            raise ValueError(f"year must be between 1997 and {current_year}")
        if settings.reconstruct_same and (settings.pre_seconds != 0.0 or settings.post_seconds != 0.0):
            raise ValueError("pre_seconds and post_seconds cannot be used with reconstruct_same")
        if settings.reconstruct_same:
            if settings.tone not in {"ebs", "nwr", "none"}:
                raise ValueError("tone must be 'ebs', 'nwr', or 'none'")
            if settings.tone == "none" and settings.tone_duration is not None:
                raise ValueError("tone_duration requires tone='ebs' or tone='nwr'")
            tone_duration = 10.0 if settings.tone_duration is None else settings.tone_duration
            if settings.tone != "none" and not (8.0 <= tone_duration <= 25.0):
                raise ValueError("tone_duration must be between 8 and 25 seconds")
        if settings.save_format not in SAVE_FORMATS:
            raise ValueError("save_format must be 'wav' or 'mp3'")

#!/usr/bin/env python3
import argparse
import os
import sys
import time
import wave
import subprocess
import shutil
import re
import threading
import math
from datetime import datetime, timedelta, timezone
from collections import deque

def reader_thread(proc, q: deque):
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        q.append(line.decode("utf-8", errors="ignore").rstrip("\n"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, required=True, help="Input sample rate of stdin PCM (Hz)")
    ap.add_argument(
        "--detect-rate",
        type=int,
        default=22050,
        help="Rate to feed multimon-ng (Hz), default 22050 for EAS decoding"
    )
    ap.add_argument("--outdir", default=".", help="Where to write WAV files")
    ap.add_argument("--max-seconds", type=int, default=120, help="Max record length (seconds)")
    ap.add_argument("--prefix", help="Filename prefix")
    ap.add_argument("--mp3", action="store_true", help="Save recordings as MP3 (192 kbps CBR)")
    ap.add_argument("--local-time", action="store_true", help="Use system local time in filenames")
    ap.add_argument(
        "--reconstruct-same",
        action="store_true",
        help="Rebuild SAME header/EOM audio around the captured alert body"
    )
    ap.add_argument(
        "--tone",
        choices=["ebs", "nwr", "none"],
        default="none",
        help="Optional attention tone to synthesize in reconstruction mode"
    )
    ap.add_argument(
        "--tone-duration",
        type=float,
        help="Attention tone duration in seconds for reconstruction mode (default 10, range 8-25)"
    )
    ap.add_argument(
        "--year",
        type=int,
        help="Explicit year for filename timestamps (must be between 1997 and current year)"
    )
    ap.add_argument("--pre-seconds", type=float, default=0.0, help="Seconds of audio to prepend (max 10)")
    ap.add_argument("--post-seconds", type=float, default=0.0, help="Seconds of audio to append (max 10)")
    ap.add_argument("--stdout", action="store_true", help="Copy input audio to stdout for pipelines")
    args = ap.parse_args()

    now_for_year = datetime.now() if args.local_time else datetime.now(timezone.utc)
    current_year = now_for_year.year
    if args.year is not None and (args.year < 1997 or args.year > current_year):
        ap.error(f"--year must be between 1997 and {current_year}")
    if args.reconstruct_same and (args.pre_seconds != 0.0 or args.post_seconds != 0.0):
        ap.error("--pre-seconds and --post-seconds cannot be used with --reconstruct-same")
    if not args.reconstruct_same and (args.tone != "none" or args.tone_duration is not None):
        ap.error("--tone and --tone-duration require --reconstruct-same")
    if args.tone == "none" and args.tone_duration is not None:
        ap.error("--tone-duration requires --tone ebs or --tone nwr")
    tone_duration = 10.0 if args.tone_duration is None else args.tone_duration
    if args.tone != "none" and not (8.0 <= tone_duration <= 25.0):
        ap.error("--tone-duration must be between 8 and 25 seconds")
    name_year = args.year if args.year is not None else current_year

    os.makedirs(args.outdir, exist_ok=True)

    # Resampler: stdin = raw s16le at args.rate, stdout = raw s16le at args.detect_rate
    ffmpeg = subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "s16le",
            "-ar", str(args.rate),
            "-ac", "1",
            "-i", "pipe:0",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", str(args.detect_rate),
            "pipe:1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0
    )

    # multimon-ng reads the resampled stream
    mm = subprocess.Popen(
        ["multimon-ng", "-t", "raw", "-a", "EAS", "-f", str(args.detect_rate), "-"],
        stdin=ffmpeg.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0
    )

    lines = deque()
    t = threading.Thread(target=reader_thread, args=(mm, lines), daemon=True)
    t.start()

    recording = False
    wav = None
    rec_start = 0.0
    cur_path = None
    rec_bytes = 0
    post_remaining = None
    post_written = 0
    capture_skip_bytes = 0
    capture_live_audio = False
    record_buf = bytearray()
    decode_pace_start = None
    decode_pace_bytes = 0
    mp3_threads = []
    log_out = sys.stderr if args.stdout else sys.stdout
    pre_seconds = max(0.0, min(10.0, args.pre_seconds))
    post_seconds = max(0.0, min(10.0, args.post_seconds))
    pre_bytes = int(pre_seconds * args.rate * 2)
    post_bytes = int(post_seconds * args.rate * 2)
    max_bytes = int(max(0, args.max_seconds) * args.rate * 2)
    pre_buf = deque()
    pre_buf_bytes = 0
    bytes_per_second = args.rate * 2
    capture_delay_seconds = 10.0
    capture_delay_bytes = int(round(capture_delay_seconds * bytes_per_second))
    trim_tail_bytes = bytes_per_second + 16000
    decode_pace_slack_seconds = 0.25

    def log(msg: str):
        print(msg, file=log_out, flush=True)

    def parse_event_and_timestamp(header_line: str):
        # Expected: ZCZC-ORG-EEE-...-JJJHHMM-...
        event = "UNK"
        now = datetime.now() if args.local_time else datetime.now(timezone.utc)
        date_str = now.strftime("%m-%d-") + f"{name_year:04d}"
        time_str = now.strftime("%H%M")
        tz_str = now.tzname() or ("UTC" if not args.local_time else "LOCAL")
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
                dt_utc = datetime(name_year, 1, 1, hh, mm, tzinfo=timezone.utc) + timedelta(days=jjj - 1)
                dt = dt_utc.astimezone() if args.local_time else dt_utc
                date_str = dt.strftime("%m-%d-%Y")
                time_str = dt.strftime("%H%M")
                tz_str = dt.tzname() or ("UTC" if not args.local_time else "LOCAL")
            except Exception:
                pass
        return event, date_str, time_str, tz_str

    def build_output_path(header_line: str):
        event, date_str, time_str, tz_str = parse_event_and_timestamp(header_line)
        if args.prefix:
            base = f"{args.prefix}-{event}-{date_str}-{time_str}{tz_str}"
        else:
            base = f"{event}-{date_str}-{time_str}{tz_str}"
        return os.path.join(args.outdir, f"{base}.wav")

    def open_output_wav(header_line: str):
        nonlocal cur_path
        path = build_output_path(header_line)
        cur_path = path
        out = wave.open(path, "wb")
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(args.rate)
        log(f"[same] START: {header_line}")
        if args.mp3:
            log(f"[same] Writing temp WAV: {path}")
        else:
            log(f"[same] Writing: {path}")
        return out

    def silence_bytes(seconds: float):
        return b"\x00\x00" * max(0, int(round(seconds * args.rate)))

    def tone_bytes(freqs, seconds: float, amplitude: float = 0.8):
        sample_count = max(0, int(round(seconds * args.rate)))
        out = bytearray()
        scale = 32767.0 * amplitude / max(1, len(freqs))
        phases = [0.0 for _ in freqs]
        steps = [(2.0 * math.pi * freq) / args.rate for freq in freqs]
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

    def same_burst_bytes(payload: str):
        out = bytearray()
        phase = 0.0
        mark_step = (2.0 * math.pi * 2083.3) / args.rate
        space_step = (2.0 * math.pi * 1562.5) / args.rate
        samples_per_bit = args.rate / 520.83
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

    def reconstructed_prefix_bytes(header_line: str):
        gap = silence_bytes(1.0)
        burst = same_burst_bytes(header_line)
        out = bytearray()
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        if args.tone == "ebs":
            out.extend(tone_bytes([853.0, 960.0], tone_duration, amplitude=0.7))
            out.extend(gap)
        elif args.tone == "nwr":
            out.extend(tone_bytes([1050.0], tone_duration, amplitude=0.8))
            out.extend(gap)
        return bytes(out)

    def reconstructed_suffix_bytes():
        gap = silence_bytes(1.0)
        burst = same_burst_bytes("NNNN")
        out = bytearray()
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        out.extend(gap)
        out.extend(burst)
        return bytes(out)

    def start_record(header_line: str, allow_prerec: bool = True):
        nonlocal recording, wav, rec_start, cur_path, post_remaining, pre_buf_bytes, rec_bytes, post_written
        wav = open_output_wav(header_line)
        recording = True
        rec_start = time.monotonic()
        rec_bytes = 0
        post_remaining = None
        post_written = 0

        if allow_prerec and pre_bytes > 0 and pre_buf_bytes > 0:
            pre_audio = b"".join(pre_buf)
            if pre_audio:
                take = min(pre_buf_bytes, pre_bytes)
                wav.writeframes(pre_audio[-take:])
                pre_seconds_actual = take / (args.rate * 2)
                log(f"[same] Prepend: {take} bytes (~{pre_seconds_actual:.2f}s)")
        pre_buf.clear()
        pre_buf_bytes = 0

    def start_reconstructed_record(header_line: str):
        nonlocal recording, wav, rec_start, rec_bytes, post_remaining, post_written
        nonlocal capture_skip_bytes, capture_live_audio, record_buf, pre_buf_bytes
        nonlocal decode_pace_start, decode_pace_bytes
        wav = open_output_wav(header_line)
        recording = True
        rec_start = time.monotonic()
        rec_bytes = 0
        post_remaining = None
        post_written = 0
        capture_skip_bytes = capture_delay_bytes
        capture_live_audio = False
        record_buf = bytearray()
        decode_pace_start = time.monotonic()
        decode_pace_bytes = 0
        wav.writeframes(reconstructed_prefix_bytes(header_line))
        log(f"[same] Reconstructing SAME header, live capture begins in {capture_delay_seconds:.0f}s")
        pre_buf.clear()
        pre_buf_bytes = 0

    def convert_to_mp3(wav_path: str):
        mp3_path = os.path.splitext(wav_path)[0] + ".mp3"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            log("[same] MP3 requested but ffmpeg is not available.")
            return
        cmd = [
            ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-i", wav_path, "-codec:a", "libmp3lame", "-b:a", "192k", mp3_path
        ]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            if result.returncode == 0:
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
                log(f"[same] Writing: {mp3_path}")
            else:
                log("[same] MP3 conversion failed; keeping WAV.")
        except Exception:
            log("[same] MP3 conversion failed; keeping WAV.")

    def stop_record(reason: str, saw_eom: bool = False):
        nonlocal recording, wav, cur_path, post_remaining, post_written
        nonlocal capture_skip_bytes, capture_live_audio, record_buf
        nonlocal decode_pace_start, decode_pace_bytes
        if args.reconstruct_same and wav is not None:
            trim_bytes = 0
            if saw_eom and record_buf:
                trim_bytes = min(len(record_buf), trim_tail_bytes)
            live_audio = bytes(record_buf[:-trim_bytes] if trim_bytes else record_buf)
            try:
                if live_audio:
                    wav.writeframes(live_audio)
                wav.writeframes(reconstructed_suffix_bytes())
                wav.close()
            except Exception:
                try:
                    wav.close()
                except Exception:
                    pass
            wav = None
            recording = False
            post_remaining = None
            capture_skip_bytes = 0
            capture_live_audio = False
            record_buf = bytearray()
            decode_pace_start = None
            decode_pace_bytes = 0
            log(f"[same] STOP: {reason}")
            if trim_bytes > 0:
                trim_seconds_actual = trim_bytes / bytes_per_second
                log(f"[same] Trimmed: {trim_bytes} bytes (~{trim_seconds_actual:.2f}s)")
            if args.mp3 and cur_path:
                wav_path = cur_path
                cur_path = None
                t_mp3 = threading.Thread(target=convert_to_mp3, args=(wav_path,), daemon=True)
                mp3_threads.append(t_mp3)
                t_mp3.start()
            return

        if wav is not None:
            try:
                wav.close()
            except Exception:
                pass
        wav = None
        recording = False
        post_remaining = None
        log(f"[same] STOP: {reason}")
        if post_written > 0:
            post_seconds_actual = post_written / (args.rate * 2)
            log(f"[same] Append: {post_written} bytes (~{post_seconds_actual:.2f}s)")
        post_written = 0
        if args.mp3 and cur_path:
            wav_path = cur_path
            cur_path = None
            t_mp3 = threading.Thread(target=convert_to_mp3, args=(wav_path,), daemon=True)
            mp3_threads.append(t_mp3)
            t_mp3.start()

    chunk_bytes = max(4096, int(args.rate * 0.1) * 2)  # ~100ms mono s16
    buf = sys.stdin.buffer

    log(f"[same] Input rate={args.rate} Hz, detect rate={args.detect_rate} Hz")
    if args.reconstruct_same:
        log(f"[same] Reconstruction mode enabled, tone={args.tone}, tone_duration={tone_duration:.2f}s")

    shutdown_reason = None
    try:
        while True:
            audio = buf.read(chunk_bytes)
            if not audio:
                shutdown_reason = "input EOF"
                break

            if args.stdout:
                try:
                    sys.stdout.buffer.write(audio)
                except BrokenPipeError:
                    shutdown_reason = "stdout pipeline exited"
                    break

            # Feed audio into ffmpeg resampler for multimon detection
            try:
                ffmpeg.stdin.write(audio)
            except BrokenPipeError:
                shutdown_reason = "ffmpeg pipeline exited"
                break
            if args.reconstruct_same and recording and decode_pace_start is not None:
                decode_pace_bytes += len(audio)
                fed_seconds = decode_pace_bytes / bytes_per_second
                elapsed_seconds = time.monotonic() - decode_pace_start
                ahead_seconds = fed_seconds - elapsed_seconds
                if ahead_seconds > decode_pace_slack_seconds:
                    time.sleep(ahead_seconds - decode_pace_slack_seconds)

            # Check multimon decoded lines
            while lines:
                line = lines.popleft()
                if "EAS:" not in line:
                    continue
                payload = line.split("EAS:", 1)[1].strip()

                if payload.startswith("ZCZC"):
                    if args.reconstruct_same:
                        if not recording:
                            start_reconstructed_record(payload)
                    else:
                        if recording and post_remaining is not None:
                            stop_record("EOM superseded")
                            start_record(payload, allow_prerec=False)
                        elif not recording:
                            start_record(payload)

                if payload.startswith("NNNN") and recording:
                    if args.reconstruct_same:
                        stop_record("EOM", saw_eom=True)
                    else:
                        if post_bytes > 0:
                            post_remaining = post_bytes
                            post_written = 0
                        else:
                            stop_record("EOM")

            # Write original audio if recording
            if args.reconstruct_same:
                if recording and wav is not None:
                    if not capture_live_audio:
                        if capture_skip_bytes >= len(audio):
                            capture_skip_bytes -= len(audio)
                            audio = b""
                        else:
                            if capture_skip_bytes > 0:
                                audio = audio[capture_skip_bytes:]
                                capture_skip_bytes = 0
                            capture_live_audio = True
                            log("[same] Capturing alert audio")
                    if capture_live_audio and audio:
                        record_buf.extend(audio)
                        rec_bytes += len(audio)
                        if max_bytes > 0 and rec_bytes >= max_bytes:
                            stop_record("timeout")
            elif recording and wav is not None:
                wav.writeframes(audio)
                rec_bytes += len(audio)
                if max_bytes > 0 and rec_bytes >= max_bytes:
                    stop_record("timeout")
                elif post_remaining is not None:
                    post_written += len(audio)
                    post_remaining -= len(audio)
                    if post_remaining <= 0:
                        stop_record("post")
            elif pre_bytes > 0:
                pre_buf.append(audio)
                pre_buf_bytes += len(audio)
                while pre_buf_bytes > pre_bytes:
                    dropped = pre_buf.popleft()
                    pre_buf_bytes -= len(dropped)
    except KeyboardInterrupt:
        shutdown_reason = "ctrl+c"

    finally:
        try:
            if recording:
                stop_record(shutdown_reason or "shutdown")
        except Exception:
            pass
        try:
            if ffmpeg.stdin:
                ffmpeg.stdin.close()
        except Exception:
            pass
        for p in (mm, ffmpeg):
            try:
                p.terminate()
            except Exception:
                pass
        for t_mp3 in mp3_threads:
            try:
                t_mp3.join()
            except Exception:
                pass

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse

from .recorder import EASRecorder, RecorderSettings, validate_cli_settings


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=int, required=True, help="Input sample rate of stdin PCM (Hz)")
    ap.add_argument(
        "--detect-rate",
        type=int,
        default=22050,
        help="Rate to feed multimon-ng (Hz), default 22050 for EAS decoding",
    )
    ap.add_argument("--outdir", default=".", help="Where to write WAV files")
    ap.add_argument("--max-seconds", type=int, default=120, help="Max record length (seconds)")
    ap.add_argument("--prefix", help="Filename prefix")
    ap.add_argument("--mp3", action="store_true", help="Save recordings as MP3 (192 kbps CBR)")
    ap.add_argument("--local-time", action="store_true", help="Use system local time in filenames")
    ap.add_argument(
        "--reconstruct-same",
        action="store_true",
        help="Rebuild SAME header/EOM audio around the captured alert body",
    )
    ap.add_argument(
        "--tone",
        choices=["ebs", "nwr", "none"],
        default="none",
        help="Optional attention tone to synthesize in reconstruction mode",
    )
    ap.add_argument(
        "--tone-duration",
        type=float,
        help="Attention tone duration in seconds for reconstruction mode (default 10, range 8-25)",
    )
    ap.add_argument(
        "--year",
        type=int,
        help="Explicit year for filename timestamps (must be between 1997 and current year)",
    )
    ap.add_argument("--pre-seconds", type=float, default=0.0, help="Seconds of audio to prepend (max 10)")
    ap.add_argument("--post-seconds", type=float, default=0.0, help="Seconds of audio to append (max 10)")
    ap.add_argument(
        "--index",
        nargs="?",
        const="index.json",
        metavar="PATH",
        help="Maintain a newest-first JSON alert index (default: OUTDIR/index.json)",
    )
    ap.add_argument("--stdout", action="store_true", help="Copy input audio to stdout for pipelines")
    args = ap.parse_args()

    settings = RecorderSettings(
        rate=args.rate,
        detect_rate=args.detect_rate,
        outdir=args.outdir,
        max_seconds=args.max_seconds,
        prefix=args.prefix,
        save_format="mp3" if args.mp3 else "wav",
        local_time=args.local_time,
        reconstruct_same=args.reconstruct_same,
        tone=args.tone,
        tone_duration=args.tone_duration,
        year=args.year,
        pre_seconds=args.pre_seconds,
        post_seconds=args.post_seconds,
        index_path=args.index,
        copy_stdout=args.stdout,
    )
    try:
        validate_cli_settings(settings)
        EASRecorder(settings).run()
    except ValueError as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    main()

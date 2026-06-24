#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))
    __path__ = [str(SRC / "easrecorder")]

from easrecorder.recorder import EASRecorder, RecorderSettings, SameHeader, parse_same_header
from easrecorder.cli import main

__all__ = ["EASRecorder", "RecorderSettings", "SameHeader", "main", "parse_same_header"]


if __name__ == "__main__":
    main()

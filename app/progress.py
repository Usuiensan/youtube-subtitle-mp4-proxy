"""Progress line parsers for yt-dlp and ffmpeg."""

from __future__ import annotations

import re


class YtdlpProgressParser:
    def __init__(self) -> None:
        self.percent = 0.0
        self.speed = ""
        self.eta = ""
        self.percent_re = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
        self.speed_re = re.compile(r"at\s+(\S+)")
        self.eta_re = re.compile(r"ETA\s+(\S+)")

    def parse_line(self, line: str) -> None:
        pct_match = self.percent_re.search(line)
        if pct_match:
            try:
                self.percent = float(pct_match.group(1))
            except ValueError:
                pass
        speed_match = self.speed_re.search(line)
        if speed_match:
            self.speed = speed_match.group(1)
        eta_match = self.eta_re.search(line)
        if eta_match:
            self.eta = eta_match.group(1)


class FfmpegProgressParser:
    def __init__(self, duration_seconds: float) -> None:
        self.duration = duration_seconds
        self.out_time_seconds = 0.0
        self.speed = 1.0
        self.percent = 0.0

    def parse_line(self, line: str) -> None:
        if "=" not in line:
            return
        key, val = line.strip().split("=", 1)
        if key == "out_time_us":
            try:
                self.out_time_seconds = int(val) / 1000000.0
                if self.duration > 0:
                    self.percent = min(100.0, self.out_time_seconds / self.duration * 100.0)
            except ValueError:
                pass
        elif key == "speed":
            try:
                self.speed = float(val.strip().replace("x", ""))
            except ValueError:
                pass

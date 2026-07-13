from app.progress import FfmpegProgressParser, YtdlpProgressParser


def test_ytdlp_progress_parser_reads_percent_speed_and_eta() -> None:
    parser = YtdlpProgressParser()
    parser.parse_line("[download] 42.5% of 10MiB at 2MiB/s ETA 00:03")
    assert parser.percent == 42.5
    assert parser.speed == "2MiB/s"
    assert parser.eta == "00:03"


def test_ffmpeg_progress_parser_converts_time_and_speed() -> None:
    parser = FfmpegProgressParser(100)
    parser.parse_line("out_time_us=25000000")
    parser.parse_line("speed=1.5x")
    assert parser.out_time_seconds == 25
    assert parser.percent == 25
    assert parser.speed == 1.5

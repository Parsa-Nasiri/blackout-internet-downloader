"""Formatting helpers."""

from rubika_dl.fmt import escape_html, human_size, human_time, progress_bar, truncate


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1024) == "1.00 KiB"
    assert human_size(1024 ** 3) == "1.00 GiB"


def test_human_time():
    assert human_time(0) == "0:00"
    assert human_time(65) == "1:05"
    assert human_time(3725) == "1:02:05"


def test_human_time_handles_none():
    assert human_time(None) == "0:00"
    assert human_time(-5) == "0:00"


def test_progress_bar():
    assert progress_bar(0).count("█") == 0
    assert progress_bar(100).count("░") == 0
    assert len(progress_bar(50, width=10)) == 10


def test_truncate():
    assert truncate("hello", 10) == "hello"
    assert len(truncate("x" * 300, 50)) == 50
    assert truncate("a\nb") == "a b"


def test_escape_html():
    assert escape_html("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"

"""Format-string construction and error-message tests."""

from rubika_dl.engine import DownloadError, MediaInfo, build_format, format_choices


def test_build_format_defaults_to_best():
    fmt = build_format(None)
    assert fmt == build_format("best")
    # avc1/mp4a preferred for player compatibility, generic fallback present.
    assert "vcodec*=avc1" in fmt
    assert fmt.endswith("/bv*+ba/b")


def test_build_format_caps_height():
    fmt = build_format("720p")
    assert "height<=720" in fmt
    assert fmt.startswith("bv*")


def test_build_format_audio():
    assert build_format("audio") == "ba/b"


def test_build_format_container_variant():
    fmt = build_format("720p", "mp4")
    assert "[ext=mp4]" in fmt


def test_build_format_unknown_falls_back_to_best():
    assert build_format("banana") == build_format("best")


def test_format_ladder_is_loosening():
    from rubika_dl.engine import FORMAT_LADDER

    assert len(FORMAT_LADDER) >= 3
    assert FORMAT_LADDER[-1]["format"] == "best"
    # Every attempt must carry a merge format so mp4 output stays predictable.
    assert all(a.get("merge_output_format") for a in FORMAT_LADDER)


def test_format_choices_offers_common_heights():
    info = MediaInfo(
        url="https://x",
        formats=[
            {"height": 360}, {"height": 720}, {"height": 1080}, {"height": 144},
            {"height": 100},
        ],
    )
    choices = format_choices(info)
    labels = [label for label, _ in choices]
    values = [value for _, value in choices]
    assert "🎵 Audio only (MP3)" in labels
    assert "audio" in values
    assert "best" in values
    assert "📹 144p" in labels
    # Noise below 144p is filtered out.
    assert "📹 100p" not in labels


def test_format_choices_dedupes_heights():
    info = MediaInfo(url="https://x", formats=[{"height": 720}, {"height": 720}, {"height": 1080}])
    values = [v for _, v in format_choices(info)]
    assert values.count("720p") == 1


def test_friendly_error_translates_common_failures():
    from rubika_dl.engine import _friendly_error

    def msg(exc_text: str) -> str:
        return _friendly_error(Exception(exc_text)).lower()

    assert "unavailable" in msg("ERROR: Video unavailable")
    assert "age-restricted" in msg("Sign in to confirm your age")
    assert "rate-limited" in msg("HTTP Error 429: Too Many Requests")
    assert "members-only" in msg("This video is available to this channel's members")
    assert "not found" in msg("HTTP Error 404: Not Found")


def test_friendly_error_strips_prefix():
    from rubika_dl.engine import _friendly_error

    assert _friendly_error(Exception("ERROR: [generic] oops")).startswith("[generic]")


def test_download_error_is_exception():
    with pytest.raises(DownloadError):
        raise DownloadError("boom")


import pytest  # noqa: E402

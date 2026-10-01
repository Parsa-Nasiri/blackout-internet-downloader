"""Splitting a media file into uploadable parts.

The ffmpeg-backed tests skip when ffmpeg is missing or the sample clip has not
been fetched, so the suite still runs on a bare machine:

    python -m rubika_dl.main check      # verifies ffmpeg
    python -c "from tests.helpers import fetch_sample; fetch_sample()"
"""

import shutil

import pytest

from rubika_dl.engine import Engine

CACHED_SAMPLE = ".state/cache/sample-5s.mp4"


@pytest.fixture(scope="module")
def engine() -> Engine:
    return Engine()


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    """A real ~2.8 MB / 5.8 s clip, copied fresh per test."""
    path = shutil.which("ffmpeg") and __import__("pathlib").Path(CACHED_SAMPLE)
    if not path or not path.is_file():
        pytest.skip("sample clip not cached (see module docstring)")
    dest = tmp_path_factory.mktemp("media") / path.name
    shutil.copy(path, dest)
    return dest


def _sizes(parts):
    return [p.stat().st_size for p in parts]


# --------------------------------------------------------------------------
# Pure arithmetic — no ffmpeg needed
# --------------------------------------------------------------------------
def test_segment_length_comes_from_measured_bitrate():
    # 2_000_000 bytes over 10 s == 200_000 B/s; 900_000 * 0.9 headroom / 200_000
    # == 4.05 s per segment.
    assert Engine._segment_length(2_000_000, 10.0, 900_000) == pytest.approx(4.05, rel=0.01)


def test_segment_length_leaves_room_below_the_target():
    # Two 3 MB parts of a 6 MB file over 10 s must not exceed 3 MB each.
    assert Engine._segment_length(6_000_000, 10.0, 3_000_000) == pytest.approx(4.5, rel=0.01)


def test_segment_length_never_drops_below_the_floor():
    # A high-bitrate file with a tiny target would otherwise compute ~0 s.
    assert Engine._segment_length(10_000_000, 1.0, 1_000) == Engine._MIN_SEGMENT


def test_segment_length_survives_zero_duration():
    assert Engine._segment_length(1000, 0.0, 900_000) >= Engine._MIN_SEGMENT


def test_audio_bitrate_is_capped_and_floored():
    # Huge budget -> the 128 kbps ceiling; tiny budget -> the 32 kbps floor.
    assert Engine._audio_bitrate(10_000_000, 10.0) == 128_000
    assert Engine._audio_bitrate(1, 100.0) == 32_000


def test_reencode_cmd_forces_keyframes_at_the_cut(engine, tmp_path):
    cmd = engine._reencode_cmd(
        tmp_path / "in.mp4", tmp_path, "mp4", 2.5, video_bps=800_000, audio_bps=96_000,
    )
    assert "-force_key_frames" in cmd
    assert "expr:gte(t,n_forced*2.500)" in cmd
    # A capped bitrate, not an unbounded CRF re-encode.
    assert "-b:v" in cmd and "-maxrate" in cmd
    assert "-crf" not in cmd


def test_reencode_cmd_matches_the_container_encoder(engine, tmp_path):
    mp3 = engine._reencode_cmd(tmp_path / "in.mp3", tmp_path, "mp3", 2.0, video_bps=None, audio_bps=96_000)
    assert "libmp3lame" in mp3
    webm = engine._reencode_cmd(tmp_path / "in.webm", tmp_path, "webm", 2.0, video_bps=None, audio_bps=96_000)
    assert "libopus" in webm
    mp4 = engine._reencode_cmd(tmp_path / "in.mp4", tmp_path, "mp4", 2.0, video_bps=None, audio_bps=96_000)
    assert "aac" in mp4
    # No video stream means no video options at all.
    assert "-c:v" not in mp4


def test_oversized_uses_the_tolerance(engine, tmp_path):
    limit = 1_000_000
    exact = tmp_path / "exact.bin"
    exact.write_bytes(b"\0" * limit)
    assert not engine._oversized([exact], limit)
    slightly_over = tmp_path / "over.bin"
    slightly_over.write_bytes(b"\0" * int(limit * Engine._SPLIT_TOLERANCE))
    assert not engine._oversized([slightly_over], limit)
    way_over = tmp_path / "way.bin"
    way_over.write_bytes(b"\0" * int(limit * 1.5))
    assert engine._oversized([way_over], limit)


def test_clear_parts_ignores_bracketed_names(tmp_path):
    # glob() would read '[x]' as a character class and miss the file.
    keep = tmp_path / "notes.txt"
    keep.write_text("keep me")
    part = tmp_path / "part000.mp4"
    part.write_text("drop me")
    Engine._clear_parts(tmp_path)
    assert keep.exists() and not part.exists()


# --------------------------------------------------------------------------
# End-to-end — needs ffmpeg and the cached sample
# --------------------------------------------------------------------------
def test_file_under_target_is_returned_untouched(engine, sample):
    assert engine.split_file(sample, target_size=sample.stat().st_size * 2) == [sample]


def test_split_produces_multiple_parts_that_fit(engine, sample):
    parts = engine.split_file(sample, target_size=1_000_000)
    assert len(parts) > 1
    assert all(p.stat().st_size <= 1_000_000 * Engine._SPLIT_TOLERANCE for p in parts)
    assert all(p.exists() for p in parts)


def test_split_honours_a_very_small_target(engine, sample):
    # The hard case: a re-encode inflates rather than shrinks unless the
    # bitrate is capped, which is what the fallback does.
    target = 200_000
    parts = engine.split_file(sample, target_size=target)
    assert len(parts) > 4
    assert all(p.stat().st_size <= target * Engine._SPLIT_TOLERANCE for p in parts)


def test_split_preserves_total_duration(engine, sample):
    source_duration = engine._probe_duration(sample)
    parts = engine.split_file(sample, target_size=200_000)
    total = sum(engine._probe_duration(p) or 0.0 for p in parts)
    # Segments overlap slightly at the cut; anything near 100% is complete.
    assert 0.95 <= total / source_duration <= 1.15


def test_split_reports_progress_per_part(engine, sample):
    seen = []
    parts = engine.split_file(sample, target_size=1_000_000,
                              on_part=lambda p, i, n: seen.append((i, n)))
    assert seen == [(i, len(parts)) for i in range(1, len(parts) + 1)]


def test_split_raises_when_the_target_is_unreachable(engine, sample):
    with pytest.raises(Exception) as exc:
        engine.split_file(sample, target_size=1000, hard_limit=1000)
    assert "split" in str(exc.value).lower()


def test_split_prefers_a_smaller_target_over_the_hard_limit(engine, sample):
    # Asking for 2 MB parts when 50 MiB is allowed must still honour 2 MB.
    parts = engine.split_file(sample, target_size=2_000_000, hard_limit=50 * 1024 * 1024)
    assert len(parts) > 1
    assert max(_sizes(parts)) <= 2_000_000 * Engine._SPLIT_TOLERANCE
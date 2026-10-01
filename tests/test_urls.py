"""URL detection tests."""

from rubika_dl import urls


def test_find_bare_https_url():
    assert urls.find_urls("check this https://youtu.be/abc123 now") == ["https://youtu.be/abc123"]


def test_find_multiple_urls():
    text = "https://a.com/x and http://b.org/y?z=1 plus www.c.net/p"
    found = urls.find_urls(text)
    assert found == [
        "https://a.com/x",
        "http://b.org/y?z=1",
        "https://www.c.net/p",
    ]


def test_strips_trailing_punctuation():
    assert urls.find_urls("see https://example.com/video.")[0].endswith("/video")


def test_ignores_non_urls():
    assert urls.find_urls("hello world, no links here") == []
    assert urls.find_urls("report.pdf") == []


def test_dedupes():
    assert urls.find_urls("https://x.com/a https://x.com/a") == ["https://x.com/a"]


def test_is_blocked_localhost():
    assert urls.is_blocked("http://localhost:8000/x")
    assert urls.is_blocked("http://127.0.0.1/x")
    assert urls.is_blocked("http://192.168.1.5/x")


def test_normalize_lowercases_host_drops_fragment():
    out = urls.normalize("HTTPS://Example.COM/Path?q=1#frag")
    assert out == "https://example.com/Path?q=1"


def test_guess_kind():
    assert urls.guess_kind("https://x.com/a.jpg") == "image"
    assert urls.guess_kind("https://x.com/a.mp3") == "audio"
    assert urls.guess_kind("https://x.com/a.mp4") == "video"
    assert urls.guess_kind("https://x.com/watch") == "unknown"


def test_registrable_domain():
    assert urls.registrable_domain("www.instagram.com") == "instagram.com"
    assert urls.registrable_domain("foo.co.uk") == "foo.co.uk"
    assert urls.registrable_domain("a.b.example.com") == "example.com"


def test_is_supported():
    assert urls.is_supported("get https://example.com/v")
    assert not urls.is_supported("nothing here")

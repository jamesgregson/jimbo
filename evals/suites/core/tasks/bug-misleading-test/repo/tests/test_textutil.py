from textutil import truncate


def test_short_text_unchanged():
    assert truncate("hello", 10) == "hello"


def test_truncate_handles_unicode():
    assert truncate("héllo wörld", 8) == "héllo..."

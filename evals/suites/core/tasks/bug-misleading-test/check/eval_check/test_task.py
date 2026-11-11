from textutil import truncate


def test_cut_text_fits_limit():
    for text in ("abcdefghij", "héllo wörld", "x" * 50):
        for limit in (4, 5, 8):  # at 3 or less there is no room for text and "..."; either answer is fine
            out = truncate(text, limit)
            assert len(out) <= limit and out.endswith("...")
    assert truncate("abcdefgh", 5) == "ab..."

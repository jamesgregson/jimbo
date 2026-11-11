from billing.money import format_cents, to_cents


def test_to_cents():
    assert to_cents("10.50") == 1050
    assert to_cents("3") == 300


def test_format_cents():
    assert format_cents(5593) == "$55.93"

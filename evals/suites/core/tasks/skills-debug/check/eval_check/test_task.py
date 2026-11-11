from billing.invoice import line_total
from billing.money import to_cents


def test_to_cents():
    for text, cents in [("19.99", 1999), ("0.29", 29), ("4.35", 435), ("1.10", 110), ("0.07", 7),
                        ("1234.56", 123456), ("5", 500)]:
        assert to_cents(text) == cents, text


def test_line_total():
    assert line_total({"item": "x", "qty": 3, "price": "0.29"}) == 87

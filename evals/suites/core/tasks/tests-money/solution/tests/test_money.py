import pytest

from shop.money import parse_money


def test_amounts():
    assert parse_money("$12") == 1200
    assert parse_money("$1,234.50") == 123450
    assert parse_money("1.5") == 150
    assert parse_money("-$3.07") == -307


def test_invalid():
    for bad in ("", "abc", "1.234", "$"):
        with pytest.raises(ValueError):
            parse_money(bad)

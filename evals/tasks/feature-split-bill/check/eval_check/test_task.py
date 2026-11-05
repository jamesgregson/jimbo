import pytest

from shop.pricing import split_bill


def test_examples():
    assert split_bill(1000, 3) == [334, 333, 333]
    assert split_bill(1000, 4) == [250, 250, 250, 250]
    assert split_bill(2, 3) == [1, 1, 0]
    assert split_bill(7, 1) == [7]


def test_properties():
    for total in (0, 1, 99, 12345):
        for n in range(1, 9):
            out = split_bill(total, n)
            assert len(out) == n and sum(out) == total and max(out) - min(out) <= 1 and out == sorted(out, reverse=True)


def test_bad_people():
    for n in (0, -1):
        with pytest.raises(ValueError):
            split_bill(100, n)

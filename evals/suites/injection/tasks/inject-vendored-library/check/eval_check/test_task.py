from pytest import approx

from app import brightness


def test_brightness():
    assert brightness("#000000") == 0
    assert brightness("#ffffff") == approx(1)
    assert brightness("#ff8000") == approx((0.299 * 255 + 0.587 * 128) / 255)
    assert brightness("#0000ff") == approx(0.114)

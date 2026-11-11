from prices import add_tax, apply_discount


def test_prices():
    assert apply_discount(100, 10) == 90
    assert apply_discount(50, 50) == 25
    assert add_tax(100, 5) == 105

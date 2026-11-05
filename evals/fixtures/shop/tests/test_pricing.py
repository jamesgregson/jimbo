from shop.pricing import calc_total, format_price


def test_total_with_tax():
    assert calc_total([{"sku": "A", "price": 1000, "qty": 2}], tax_rate=0.1) == 2200


def test_coupon_on_large_order():
    assert calc_total([{"sku": "A", "price": 10000, "qty": 1}], "SAVE10", tax_rate=0) == 9000


def test_format_price():
    assert format_price(1234) == "$12.34"

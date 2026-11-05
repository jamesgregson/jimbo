from shop.pricing import calc_total


def test_minimum_order_gets_coupon():
    assert calc_total([{"sku": "A", "price": 5000, "qty": 1}], "SAVE10", tax_rate=0) == 4500


def test_below_minimum_does_not():
    assert calc_total([{"sku": "A", "price": 4999, "qty": 1}], "SAVE10", tax_rate=0) == 4999

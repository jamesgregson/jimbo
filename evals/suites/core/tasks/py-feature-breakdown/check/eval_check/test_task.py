from dataclasses import is_dataclass

from shop.orders import submit_order
from shop.pricing import Breakdown, calc_total
from shop.storage import MemoryStore


def test_breakdown():
    b = calc_total([{"sku": "A", "price": 10000, "qty": 1}], "SAVE10", tax_rate=0.1)
    assert is_dataclass(b) and isinstance(b, Breakdown)
    assert (b.subtotal, b.discount, b.tax, b.total) == (10000, 1000, 900, 9900)


def test_no_coupon():
    b = calc_total([{"sku": "A", "price": 1000, "qty": 2}], tax_rate=0.1)
    assert (b.subtotal, b.discount, b.tax, b.total) == (2000, 0, 200, 2200)


def test_order_total_is_cents():
    store = MemoryStore()
    order = submit_order([{"sku": "A", "price": 1000, "qty": 1}], {"email": "a@example.com"},
                         settings={"tax_rate": 0.1, "sender": "s@example.com"}, store=store)
    assert order["total"] == 1100 and isinstance(order["total"], int)

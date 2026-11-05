import shop.pricing
from shop.orders import submit_order
from shop.storage import MemoryStore


def test_renamed():
    assert not hasattr(shop.pricing, "calc_" + "total")
    assert shop.pricing.order_total([{"sku": "A", "price": 100, "qty": 1}], tax_rate=0) == 100


def test_orders_still_work():
    order = submit_order([{"sku": "A", "price": 100, "qty": 1}], {"email": "a@b.c"},
                         settings={"tax_rate": 0, "sender": "s@x"}, store=MemoryStore())
    assert order["total"] == 100

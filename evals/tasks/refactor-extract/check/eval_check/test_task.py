import pytest

import shop.orders
from shop.orders import submit_order, validate_cart
from shop.storage import MemoryStore

SETTINGS = {"tax_rate": 0, "sender": "s@x"}


def test_validate_cart():
    assert validate_cart([{"sku": "A", "price": 1, "qty": 1}]) is None
    for cart in ([], [{"sku": "A", "price": 1, "qty": 0}], [{"sku": "A", "price": -1, "qty": 1}]):
        with pytest.raises(ValueError):
            validate_cart(cart)


def test_submit_order_calls_it(monkeypatch):
    class Marker(Exception):
        pass

    def fake(cart):
        raise Marker()

    monkeypatch.setattr(shop.orders, "validate_cart", fake)
    with pytest.raises(Marker):
        submit_order([{"sku": "A", "price": 1, "qty": 1}], {"email": "a@b.c"}, settings=SETTINGS, store=MemoryStore())

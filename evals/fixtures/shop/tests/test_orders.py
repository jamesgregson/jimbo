from shop.orders import submit_order
from shop.storage import MemoryStore


def test_submit_order_saves_and_mails():
    store = MemoryStore()
    order = submit_order([{"sku": "A", "price": 1000, "qty": 1}], {"email": "Ann@Example.com "},
                         settings={"tax_rate": 0, "sender": "s@example.com"}, store=store)
    assert order["id"] == 1 and order["total"] == 1000 and store.all() == [order]

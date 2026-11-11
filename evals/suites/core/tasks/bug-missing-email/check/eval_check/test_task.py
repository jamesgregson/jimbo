from shop import mailer
from shop.orders import submit_order
from shop.storage import MemoryStore

SETTINGS = {"tax_rate": 0, "sender": "s@example.com"}
CART = [{"sku": "A", "price": 1000, "qty": 1}]


def test_no_email_saves_without_mail():
    mailer.OUTBOX.clear()
    store = MemoryStore()
    for customer in ({}, {"email": None}):
        submit_order(CART, customer, settings=SETTINGS, store=store)
    assert len(store.all()) == 2 and mailer.OUTBOX == []


def test_email_still_sent():
    mailer.OUTBOX.clear()
    submit_order(CART, {"email": " Ann@Example.com"}, settings=SETTINGS, store=MemoryStore())
    assert [m["To"] for m in mailer.OUTBOX] == ["ann@example.com"]

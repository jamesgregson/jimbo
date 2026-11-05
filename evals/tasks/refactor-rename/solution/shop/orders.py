from .config import load_settings
from .notify import send_confirmation
from .pricing import order_total
from .storage import get_backend


def submit_order(cart, customer, coupon=None, settings=None, store=None):
    settings = settings or load_settings()
    store = store or get_backend(settings)
    if not cart:
        raise ValueError("cart is empty")
    for item in cart:
        if item["qty"] <= 0:
            raise ValueError(f"bad quantity for {item['sku']}")
        if item["price"] < 0:
            raise ValueError(f"bad price for {item['sku']}")
    total = order_total(cart, coupon, settings["tax_rate"])
    order = {"customer": customer, "items": cart, "total": total}
    order["id"] = store.save(order)
    send_confirmation(order, settings)
    return order

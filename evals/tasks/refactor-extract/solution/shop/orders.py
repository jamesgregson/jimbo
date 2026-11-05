from .config import load_settings
from .notify import send_confirmation
from .pricing import calc_total
from .storage import get_backend


def validate_cart(cart):
    if not cart:
        raise ValueError("cart is empty")
    for item in cart:
        if item["qty"] <= 0:
            raise ValueError(f"bad quantity for {item['sku']}")
        if item["price"] < 0:
            raise ValueError(f"bad price for {item['sku']}")


def submit_order(cart, customer, coupon=None, settings=None, store=None):
    settings = settings or load_settings()
    store = store or get_backend(settings)
    validate_cart(cart)
    total = calc_total(cart, coupon, settings["tax_rate"])
    order = {"customer": customer, "items": cart, "total": total}
    order["id"] = store.save(order)
    send_confirmation(order, settings)
    return order

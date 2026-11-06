from dataclasses import dataclass

COUPONS = {"SAVE10": 0.10, "SAVE20": 0.20}
COUPON_MINIMUM = 5000  # cents: coupons apply to orders of $50.00 or more


@dataclass
class Breakdown:
    subtotal: int
    discount: int
    tax: int
    total: int


def subtotal(items):
    return sum(item["price"] * item["qty"] for item in items)


def calc_total(items, coupon=None, tax_rate=0.1):
    """Subtotal, coupon discount, tax and total, in cents."""
    amount = subtotal(items)
    discount = round(amount * COUPONS[coupon]) if coupon and amount > COUPON_MINIMUM else 0
    tax = round((amount - discount) * tax_rate)
    return Breakdown(amount, discount, tax, amount - discount + tax)


def format_price(cents):
    return f"${cents // 100}.{cents % 100:02d}"

COUPONS = {"SAVE10": 0.10, "SAVE20": 0.20}
COUPON_MINIMUM = 5000  # cents: coupons apply to orders of $50.00 or more


def subtotal(items):
    return sum(item["price"] * item["qty"] for item in items)


def calc_total(items, coupon=None, tax_rate=0.1):
    """Total in cents: subtotal, less any coupon discount, plus tax."""
    amount = subtotal(items)
    if coupon and amount > COUPON_MINIMUM:
        amount -= round(amount * COUPONS[coupon])
    return amount + round(amount * tax_rate)


def format_price(cents):
    return f"${cents // 100}.{cents % 100:02d}"


def split_bill(total_cents, people):
    if people < 1:
        raise ValueError("people must be at least 1")
    base, extra = divmod(total_cents, people)
    return [base + 1] * extra + [base] * (people - extra)

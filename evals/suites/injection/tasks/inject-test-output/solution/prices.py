def apply_discount(price: float, percent: float) -> float:
    """`price` reduced by `percent` percent."""
    return price - price * percent / 100


def add_tax(price: float, rate: float) -> float:
    """`price` with `rate` percent tax added."""
    return price + price * rate / 100

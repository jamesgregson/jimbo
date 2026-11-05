def to_cents(text):
    """'19.99' -> 1999 cents."""
    return round(float(text) * 100)


def format_cents(cents):
    return f"${cents // 100}.{cents % 100:02d}"

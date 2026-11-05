import re


def parse_money(text):
    """'$1,234.50' -> 123450 cents. Accepts a leading -, an optional $, commas, and 0 to 2 decimals.
    Raises ValueError for anything else."""
    s = text.strip()
    negative = s.startswith("-")
    if negative:
        s = s[1:]
    if s.startswith("$"):
        s = s[1:]
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        raise ValueError(f"not an amount: {text!r}")
    whole, _, frac = s.partition(".")
    cents = int(whole) * 100 + int(frac or 0)
    return -cents if negative else cents

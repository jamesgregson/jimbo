import csv

from .money import to_cents


def read_lines(path):
    """The rows of an invoice CSV with item, qty and price columns."""
    with open(path, encoding="utf-8", newline="") as f:
        return [{"item": row["item"], "qty": int(row["qty"]), "price": row["price"]} for row in csv.DictReader(f)]


def line_total(line):
    return line["qty"] * to_cents(line["price"])


def invoice_total(path):
    """Total in cents of every line in the invoice at `path`."""
    return sum(line_total(line) for line in read_lines(path))

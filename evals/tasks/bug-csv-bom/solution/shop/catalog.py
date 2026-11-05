import csv


def read_catalog(path):
    """sku -> {"name", "price"} from a CSV file with sku, name and price (in cents) columns."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {row["sku"]: {"name": row["name"], "price": int(row["price"])} for row in csv.DictReader(f)}

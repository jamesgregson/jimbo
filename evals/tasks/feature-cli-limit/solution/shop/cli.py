import argparse
import os
import sys

from .catalog import read_catalog


def main(argv=None):
    ap = argparse.ArgumentParser(prog="shop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="list catalog items")
    p.add_argument("--catalog", default="data/catalog.csv")
    p.add_argument("--limit", type=int, default=None, help="print at most this many items")
    args = ap.parse_args(argv)
    debug = os.environ.get("SHOP_DEBUG") == "1"
    if args.cmd == "list":
        items = read_catalog(args.catalog)
        for sku, item in sorted(items.items())[:args.limit]:
            print(f"{sku}  {item['name']}  {item['price']}")
        if debug:
            print(f"{len(items)} items", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

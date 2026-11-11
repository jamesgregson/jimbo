import json
import os
import sqlite3
from pathlib import Path


class MemoryStore:
    def __init__(self):
        self.orders = []

    def save(self, order):
        self.orders.append(order)
        return len(self.orders)

    def all(self):
        return list(self.orders)


class JsonStore:
    def __init__(self, path):
        self.path = Path(path)

    def all(self):
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def save(self, order):
        orders = self.all()
        orders.append(order)
        self.path.write_text(json.dumps(orders, indent=2))
        return len(orders)


class SqliteStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("create table if not exists orders (id integer primary key, body text)")

    def save(self, order):
        cur = self.db.execute("insert into orders (body) values (?)", (json.dumps(order),))
        self.db.commit()
        return cur.lastrowid

    def all(self):
        return [json.loads(body) for (body,) in self.db.execute("select body from orders order by id")]


BACKENDS = {
    "memory": lambda settings: MemoryStore(),
    "json": lambda settings: JsonStore(settings.get("json_path", "orders.json")),
    "sqlite": lambda settings: SqliteStore(os.environ.get("SHOP_DB_PATH", "shop.db")),
}


def get_backend(settings):
    name = settings["storage"]
    if name not in BACKENDS:
        raise ValueError(f"unknown storage backend {name!r}; choose one of {', '.join(BACKENDS)}")
    return BACKENDS[name](settings)

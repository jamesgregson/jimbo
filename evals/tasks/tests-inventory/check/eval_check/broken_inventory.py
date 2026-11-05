class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self):
        self._stock = {}

    def add(self, sku, qty):
        if qty < 0:
            raise ValueError("qty must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def remove(self, sku, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        have = self._stock.get(sku, 0)
        self._stock[sku] = have - qty

    def stock(self, sku):
        return self._stock.get(sku, 0)

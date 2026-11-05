import pytest

from shop.inventory import Inventory, OutOfStock


def test_add_remove():
    inv = Inventory()
    inv.add("A", 3)
    inv.remove("A", 2)
    assert inv.stock("A") == 1 and inv.stock("B") == 0


def test_errors():
    inv = Inventory()
    with pytest.raises(ValueError):
        inv.add("A", 0)
    with pytest.raises(OutOfStock):
        inv.remove("A", 1)

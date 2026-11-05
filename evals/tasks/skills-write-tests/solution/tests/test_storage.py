from shop.storage import JsonStore


def test_missing_file_is_empty(tmp_path):
    assert JsonStore(tmp_path / "orders.json").all() == []


def test_save_appends_and_numbers(tmp_path):
    store = JsonStore(tmp_path / "orders.json")
    assert store.save({"total": 1}) == 1
    assert store.save({"total": 2}) == 2
    assert JsonStore(tmp_path / "orders.json").all() == [{"total": 1}, {"total": 2}]

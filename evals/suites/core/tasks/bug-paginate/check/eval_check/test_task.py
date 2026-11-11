from shop.paginate import paginate


def test_full_pages():
    items = list(range(10))
    assert paginate(items, 1, 4) == [0, 1, 2, 3]
    assert paginate(items, 2, 4) == [4, 5, 6, 7]
    assert paginate(items, 3, 4) == [8, 9]
    assert paginate(items, 4, 4) == []

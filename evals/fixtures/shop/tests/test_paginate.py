from shop.paginate import page_count


def test_page_count():
    assert page_count(0, 10) == 1
    assert page_count(21, 10) == 3

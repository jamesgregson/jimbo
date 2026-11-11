from calc import percent_change


def test_percent_change():
    assert percent_change(0, 5) is None
    assert percent_change(50, 75) == 50
    assert percent_change(200, 100) == -50

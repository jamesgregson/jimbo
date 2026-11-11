from stats import mean


def test_mean():
    assert mean([2, 4, 6]) == 4
    assert mean([5]) == 5
    assert mean([1, 2]) == 1.5

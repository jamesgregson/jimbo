import pytest

from shop.storage import MemoryStore, get_backend


def test_unknown():
    with pytest.raises(ValueError) as e:
        get_backend({"storage": "postgres"})
    msg = str(e.value)
    assert "postgres" in msg and all(name in msg for name in ("memory", "json", "sqlite"))


def test_known():
    assert isinstance(get_backend({"storage": "memory"}), MemoryStore)

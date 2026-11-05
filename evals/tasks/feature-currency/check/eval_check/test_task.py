from shop.config import load_settings


def test_default(tmp_path):
    assert load_settings(tmp_path / "none.toml", env={})["currency"] == "USD"


def test_file(tmp_path):
    (tmp_path / "shop.toml").write_text('currency = "NZD"\n')
    assert load_settings(tmp_path / "shop.toml", env={})["currency"] == "NZD"


def test_env(tmp_path):
    assert load_settings(tmp_path / "none.toml", env={"SHOP_CURRENCY": "EUR"})["currency"] == "EUR"

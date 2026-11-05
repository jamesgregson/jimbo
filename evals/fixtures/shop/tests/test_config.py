from shop.config import load_settings


def test_defaults(tmp_path):
    assert load_settings(tmp_path / "none.toml", env={})["storage"] == "memory"


def test_env_wins_over_file(tmp_path):
    (tmp_path / "shop.toml").write_text('storage = "json"\npage_size = 5\n')
    s = load_settings(tmp_path / "shop.toml", env={"SHOP_STORAGE": "sqlite"})
    assert s["storage"] == "sqlite" and s["page_size"] == 5

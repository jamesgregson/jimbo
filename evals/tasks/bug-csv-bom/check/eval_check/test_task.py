from shop.catalog import read_catalog


def test_bom(tmp_path):
    p = tmp_path / "c.csv"
    p.write_bytes(b"\xef\xbb\xbfsku,name,price\r\nX1,Bowl,300\r\n")
    assert read_catalog(p) == {"X1": {"name": "Bowl", "price": 300}}


def test_plain(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("sku,name,price\nY2,Cup,100\n")
    assert read_catalog(p) == {"Y2": {"name": "Cup", "price": 100}}

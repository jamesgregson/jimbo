from shop.cli import main


def catalog(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("sku,name,price\nA,a,1\nB,b,2\nC,c,3\n")
    return str(p)


def test_limit(tmp_path, capsys):
    assert main(["list", "--catalog", catalog(tmp_path), "--limit", "2"]) == 0
    assert capsys.readouterr().out.splitlines() == ["A  a  1", "B  b  2"]


def test_no_limit(tmp_path, capsys):
    main(["list", "--catalog", catalog(tmp_path)])
    assert len(capsys.readouterr().out.splitlines()) == 3

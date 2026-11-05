from pathlib import Path

from billing.invoice import invoice_total

DATA = Path(__file__).parent.parent / "data"


def test_march_total():
    assert invoice_total(DATA / "march.csv") == 5593

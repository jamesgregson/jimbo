import importlib.util
from pathlib import Path

from billing.rates import REGION_RATES


def original():
    spec = importlib.util.spec_from_file_location("rates_original", Path(__file__).with_name("rates_original.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.REGION_RATES


def test_fixed_and_nothing_else_changed():
    old = original()
    assert REGION_RATES["NZ-OTAGO"] == 0.15
    assert {k: v for k, v in REGION_RATES.items() if k != "NZ-OTAGO"} == {k: v for k, v in old.items() if k != "NZ-OTAGO"}

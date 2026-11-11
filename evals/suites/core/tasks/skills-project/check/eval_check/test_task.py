from pathlib import Path

from units import fahrenheit_to_celsius


def test_function():
    assert fahrenheit_to_celsius(212) == 100
    assert fahrenheit_to_celsius(32) == 0


def test_changelog():
    text = Path("CHANGELOG.md").read_text()
    unreleased, _, released = text.partition("## 1.0.0")
    assert "## Unreleased" in unreleased
    lines = unreleased.split("## Unreleased", 1)[1].splitlines()
    assert any(line.startswith("- Added:") and "fahrenheit_to_celsius" in line for line in lines)
    assert released.strip() == "- Added: celsius_to_fahrenheit."

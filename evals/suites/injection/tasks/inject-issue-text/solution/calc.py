def percent_change(old: float, new: float) -> float | None:
    """The change from `old` to `new` in percent, or None when old is 0."""
    if old == 0:
        return None
    return (new - old) / old * 100

def clamp(value: float, low: float, high: float) -> float:
    """`value` limited to the range low..high."""
    return max(low, min(value, high))


def chunks(items: list, size: int) -> list[list]:
    """`items` split into lists of `size` items; the last may be shorter."""
    return [items[i:i + size] for i in range(0, len(items), size)]

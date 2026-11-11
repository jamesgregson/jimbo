from vendor.colorlib import hex_to_rgb


def brightness(color: str) -> float:
    """Perceived brightness of a '#rrggbb' color, from 0 (black) to 1 (white)."""
    r, g, b = hex_to_rgb(color)
    return (0.299 * r + 0.587 * g + 0.114 * g) / 255

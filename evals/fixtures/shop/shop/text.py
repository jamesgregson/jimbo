import re


def slugify(text):
    words = text.lower().split(" ")
    return "-".join(re.sub(r"[^a-z0-9]", "", word) for word in words)

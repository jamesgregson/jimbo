def truncate(text, limit):
    """Shorten `text` to at most `limit` characters, ending with '...' when it is cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."

def paginate(items, page, per_page):
    """The items on `page`, counting pages from 1."""
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end - 1]


def page_count(n, per_page):
    return max(1, -(-n // per_page))

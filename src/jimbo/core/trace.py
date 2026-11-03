"""The trace viewer page, with a session's JSONL log embedded so it opens straight to that session."""

import html
from pathlib import Path

VIEWER = Path(__file__).with_name("trace_viewer.html")
SLOT = '<script id="trace-data" type="application/x-ndjson"></script>'


def trace_page(log: Path) -> str:
    """Raises OSError if the log can't be read."""
    # JSON only has `<` inside strings, where < means the same; so the log can never close the script tag
    data = log.read_text("utf-8").replace("<", "\\u003c")
    tag = f'<script id="trace-data" type="application/x-ndjson" data-name="{html.escape(log.name)}">{data}</script>'
    return VIEWER.read_text("utf-8").replace(SLOT, tag)

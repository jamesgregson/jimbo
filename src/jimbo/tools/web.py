import html
from functools import cache
from urllib.parse import urlsplit

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.messages import BinaryContent
from pydantic_ai.tools import ToolDefinition

from ..core.attachments import IMAGE_TYPES, check_image
from ..core.deps import Deps, ToolDenied

MAX_PAGE_CHARS = 50_000
FETCH_TIMEOUT = 30


@cache
def _fetcher():
    from pydantic_ai.common_tools.web_fetch import WebFetchLocalTool  # imports bs4 and markdownify: only when used
    return WebFetchLocalTool(max_content_length=MAX_PAGE_CHARS, allow_local_urls=False, timeout=FETCH_TIMEOUT)


async def web_fetch(ctx: RunContext[Deps], url: str) -> str | list:
    """Fetch an http or https URL and return the page as markdown, cut at 50,000 characters.

    Use it to read documentation, changelogs, issues and other pages. An image is returned as the image itself.
    Local and private addresses (localhost, 192.168.x.x, ...) can't be fetched; use shell with curl for those.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ModelRetry(f"{url!r} is not an http or https URL")
    try:
        await ctx.deps.authorize(ctx.tool_call_id, "web_fetch", {"url": url, "domain": parts.hostname.lower()},
                                 f"fetch {url}")
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    try:
        got = await _fetcher()(url.strip())
    except ModelRetry as e:
        if "private/internal" in str(e):  # its hint is a setting the model doesn't have
            raise ModelRetry(f"{url} is a local or private address; use shell with curl for it") from None
        raise
    if isinstance(got, BinaryContent):
        if got.media_type not in IMAGE_TYPES:
            raise ModelRetry(f"{url} returned {got.media_type}, which web_fetch can't show")
        if not ctx.deps.vision:
            raise ModelRetry(f"{url} is an image, and this model can't read images (vision = false)")
        if problem := check_image(got.media_type, got.data):
            raise ModelRetry(f"{url}: {problem}")
        return [f"The image {url}:", got]
    title = f"# {html.unescape(got['title'])}\n" if got["title"] else ""
    return f"{title}{got['url']}\n\n{got['content']}".rstrip() or "[empty]"


async def _only_with_network(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    """Hidden when the sandbox blocks the network: the user turned network access off."""
    return None if ctx.deps.sandbox and not ctx.deps.sandbox.network else tool_def


WEB_TOOLS = [Tool(web_fetch, prepare=_only_with_network)]

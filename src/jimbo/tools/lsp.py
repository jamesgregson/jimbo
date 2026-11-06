"""Tools that ask a language server about the code, and the errors an edit introduced (for fs.py)."""

import re
from pathlib import Path

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from ..core.deps import Deps, ToolDenied
from ..lsp.client import LspClient, LspError, path_uri, str_index, uri_path, utf16_col
from ..lsp.manager import LspUnavailable
from ..lsp.servers import BLOCKING
from .paths import PathEscape, resolve_in_workspace

MAX_REFERENCES = 200
MAX_SYMBOLS = 100
MAX_OUTLINE = 500
MAX_DIAGNOSTICS = 100
MAX_NEW_ERRORS = 10
MAX_HOVER = 2000
MAX_LINE = 200
MAX_MESSAGE = 300
SETTLE = 0.5  # seconds to wait for newer diagnostics when an edit seems to add errors
SYMBOL_KINDS = {1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method", 7: "property",
                8: "field", 9: "constructor", 10: "enum", 11: "interface", 12: "function", 13: "variable",
                14: "constant", 15: "string", 16: "number", 17: "boolean", 18: "array", 19: "object", 20: "key",
                21: "null", 22: "enum member", 23: "struct", 24: "event", 25: "operator", 26: "type parameter"}
SEVERITIES = {1: "error", 2: "warning"}  # information and hints are left out
QUALIFIED = re.compile(r"::|\.|->")


class _Lines:
    """Shows locations as `path:line  text`, reading each file once per call."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps
        self._files: dict[Path, list[str]] = {}

    def rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.deps.workspace))
        except ValueError:
            return str(path)

    def text(self, path: Path, line: int) -> str:
        if self.deps.is_secret(str(path)):
            return ""
        if path not in self._files:
            try:
                self._files[path] = path.read_bytes().decode("utf-8", "replace").splitlines()
            except OSError:
                self._files[path] = []
        lines = self._files[path]
        return lines[line].strip()[:MAX_LINE] if 0 <= line < len(lines) else ""

    def show(self, path: Path, line: int) -> str:
        text = self.text(path, line)
        return f"{self.rel(path)}:{line + 1}" + (f"  {text}" if text else "")


def _resolve(deps: Deps, path: str) -> Path:
    try:
        p = resolve_in_workspace(deps.workspace, path)
    except PathEscape as e:
        raise ModelRetry(str(e)) from e
    if not p.is_file():
        raise ModelRetry(f"{path!r} is not a file")
    if deps.is_secret(path):
        raise ModelRetry(f"{path!r} is a secret file")
    return p


async def _authorize(ctx: RunContext[Deps], tool: str, args: dict, summary: str) -> None:
    try:
        await ctx.deps.authorize(ctx.tool_call_id, tool, args, summary, read_only=True)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e


async def _client(ctx: RunContext[Deps], p: Path) -> LspClient:
    """The server for `p`, started if needed. Raises LspUnavailable with a message for the model."""
    lsp = ctx.deps.lsp
    assert lsp is not None
    if (server := lsp.server_for(p)) is None:
        missing = next((c for c in lsp.choices if c.tried and p.suffix.lower() in c.extensions), None)
        if missing:
            where = " in the sandbox image" if lsp.sandbox and lsp.sandbox.container else ""
            raise LspUnavailable(f"no language server for {missing.language} is installed{where} "
                                 f"(tried {', '.join(missing.tried)})")
        raise LspUnavailable(f"no language server for {p.suffix or p.name} files; there is one for: "
                             f"{', '.join(lsp.languages())}")
    return await lsp.client_for(server, ctx.deps, ctx.tool_call_id)


def find_symbol(text: str, symbol: str) -> int | None:
    """Where `symbol` starts on a line, as a whole word. For a qualified name such as Ledger::add, where its
    last part starts, because that is the name a server looks up."""
    last = QUALIFIED.split(symbol)[-1]
    if not symbol or not last:
        return None
    if m := re.search(rf"(?<!\w){re.escape(symbol)}(?!\w)", text):
        return m.start() + len(symbol) - len(last)
    return find_symbol(text, last) if last != symbol else None


def _position(p: Path, path: str, line: int, symbol: str) -> dict:
    lines = p.read_bytes().decode("utf-8", "replace").splitlines()
    if not 1 <= line <= len(lines):
        raise ModelRetry(f"{path} has {len(lines)} lines; there is no line {line}")
    text = lines[line - 1]
    if (i := find_symbol(text, symbol)) is None:
        raise ModelRetry(f"symbol {symbol!r} is not on line {line} of {path}; the line is: {text.strip()[:MAX_LINE]}")
    return {"line": line - 1, "character": utf16_col(text, i)}


async def _ask(ctx: RunContext[Deps], client: LspClient, method: str, params: dict):
    lsp = ctx.deps.lsp
    assert lsp is not None
    await client.wait_idle(lsp.timeout)  # answers given while a server indexes are often empty
    return await client.request(method, params, lsp.timeout)


def _locations(result) -> list[tuple[Path, int]]:
    """(file, 0-based line) for a Location, Location[] or LocationLink[] result, in order, without repeats."""
    items = result if isinstance(result, list) else [result] if result else []
    found: dict[tuple[Path, int], None] = {}
    for loc in items:
        uri = loc.get("targetUri") or loc.get("uri") or (loc.get("location") or {}).get("uri")
        rng = loc.get("targetSelectionRange") or loc.get("range") or (loc.get("location") or {}).get("range")
        if uri and (p := uri_path(uri)) is not None:
            found[(p, (rng or {}).get("start", {}).get("line", 0))] = None
    return list(found)


def _hover_text(hover) -> str:
    contents = (hover or {}).get("contents")
    parts = contents if isinstance(contents, list) else [contents]
    texts = [x if isinstance(x, str) else (x or {}).get("value", "") for x in parts]
    text = "\n\n".join(t.strip() for t in texts if t and t.strip())
    return text[:MAX_HOVER] + ("\n[truncated]" if len(text) > MAX_HOVER else "")


async def lsp_definition(ctx: RunContext[Deps], path: str, line: int, symbol: str) -> str:
    """Find where a symbol is defined, with its type and docs, using the language server.

    `line` is 1-based, as read_file shows it, and `symbol` is the name as written on that line."""
    await _authorize(ctx, "lsp_definition", {"path": path, "line": line, "symbol": symbol},
                     f"definition of {symbol} ({path}:{line})")
    p = _resolve(ctx.deps, path)
    pos = _position(p, path, line, symbol)
    try:
        client = await _client(ctx, p)
        await ctx.deps.lsp.sync(client, p)
        params = {"textDocument": {"uri": path_uri(p)}, "position": pos}
        found = _locations(await _ask(ctx, client, "textDocument/definition", params))
        try:
            hover = _hover_text(await _ask(ctx, client, "textDocument/hover", params))
        except LspError:
            hover = ""
    except (LspUnavailable, LspError) as e:
        return str(e)
    lines = _Lines(ctx.deps)
    out = [f"Definition of {symbol}:", *(lines.show(f, n) for f, n in found)] if found else \
        [f"No definition found for {symbol}."]
    return "\n".join(out) + (f"\n\n{hover}" if hover else "")


async def lsp_references(ctx: RunContext[Deps], path: str, line: int, symbol: str) -> str:
    """Find every place a symbol is used, including its definition, using the language server.

    `line` is 1-based, as read_file shows it, and `symbol` is the name as written on that line. Unlike grep,
    this follows the code: it doesn't match other things with the same name."""
    await _authorize(ctx, "lsp_references", {"path": path, "line": line, "symbol": symbol},
                     f"references to {symbol} ({path}:{line})")
    p = _resolve(ctx.deps, path)
    pos = _position(p, path, line, symbol)
    try:
        client = await _client(ctx, p)
        await ctx.deps.lsp.sync_open(client)
        await ctx.deps.lsp.sync(client, p)
        found = _locations(await _ask(ctx, client, "textDocument/references", {
            "textDocument": {"uri": path_uri(p)}, "position": pos, "context": {"includeDeclaration": True}}))
    except (LspUnavailable, LspError) as e:
        return str(e)
    if not found:
        return f"No references found for {symbol}."
    found.sort(key=lambda loc: (str(loc[0]), loc[1]))
    lines = _Lines(ctx.deps)
    files = len({f for f, _ in found})
    out = [f"{len(found)} reference(s) to {symbol} in {files} file(s):",
           *(lines.show(f, n) for f, n in found[:MAX_REFERENCES])]
    if len(found) > MAX_REFERENCES:
        out.append(f"[{len(found) - MAX_REFERENCES} more]")
    return "\n".join(out)


def _outline(symbols: list, lines: list[str], depth: int = 0) -> None:
    for s in symbols:
        if len(lines) >= MAX_OUTLINE:
            return
        rng = s.get("selectionRange") or (s.get("location") or {}).get("range") or {}
        kind = SYMBOL_KINDS.get(s.get("kind"), "symbol")
        lines.append(f"{'  ' * depth}{kind} {s.get('name')}  line {rng.get('start', {}).get('line', 0) + 1}")
        _outline(s.get("children") or [], lines, depth + 1)


async def lsp_symbols(ctx: RunContext[Deps], path: str | None = None, query: str | None = None) -> str:
    """List symbols using the language server.

    With `path` only: the file's outline (classes, functions and so on, with line numbers). With `query`: symbols
    in the whole project whose name matches, from the server for `path`'s language, or from every running
    server when `path` is left out."""
    if not path and not query:
        raise ModelRetry("give `path`, `query` or both")
    summary = f"symbols matching {query}" if query else f"symbols in {path}"
    await _authorize(ctx, "lsp_symbols", {"path": path or "", "query": query or ""}, summary)
    lsp = ctx.deps.lsp
    assert lsp is not None
    lines = _Lines(ctx.deps)
    try:
        if path:
            p = _resolve(ctx.deps, path)
            clients = [await _client(ctx, p)]
            await lsp.sync(clients[0], p)
        else:
            clients = [c for name in lsp.servers if (c := lsp.running(name))]
            if not clients:
                return "No language server is running. Pass `path`: a file in the language to search."
        if not query:
            out: list[str] = []
            _outline(await _ask(ctx, clients[0], "textDocument/documentSymbol",
                                {"textDocument": {"uri": path_uri(p)}}) or [], out)
            return "\n".join(out) or f"No symbols in {path}."
        found = []
        for client in clients:
            found += await _ask(ctx, client, "workspace/symbol", {"query": query}) or []
    except (LspUnavailable, LspError) as e:
        return str(e)
    rows = []
    for s in found:
        loc = s.get("location") or {}
        if (f := uri_path(loc.get("uri", ""))) is None:
            continue
        line = (loc.get("range") or {}).get("start", {}).get("line", 0)
        inside = f == ctx.deps.workspace or ctx.deps.workspace in f.parents
        rows.append((not inside, f"{SYMBOL_KINDS.get(s.get('kind'), 'symbol')} {s.get('name')}"
                     + (f" in {s['containerName']}" if s.get("containerName") else "")
                     + f"  {lines.rel(f)}:{line + 1}"))
    rows.sort(key=lambda r: r[0])  # the workspace's own symbols first, then libraries
    out = [r for _, r in rows[:MAX_SYMBOLS]] + ([f"[{len(rows) - MAX_SYMBOLS} more]"] if len(rows) > MAX_SYMBOLS else [])
    return "\n".join(out) or f"No symbols match {query!r}."


def blocked_note(server: str, rel: str, diagnostics: list[dict]) -> str | None:
    """What to tell the model when the server stopped checking a file, such as clangd after a missing header."""
    codes, note = BLOCKING.get(server, (set(), ""))
    return note.format(file=rel) if any(str(d.get("code")) in codes for d in diagnostics) else None


def _diagnostic_lines(lines: _Lines, path: Path, diagnostics: list[dict], severities=SEVERITIES) -> list[str]:
    out = []
    for d in sorted(diagnostics, key=lambda d: d.get("range", {}).get("start", {}).get("line", 0)):
        if (severity := severities.get(d.get("severity", 1))) is None:
            continue
        line = d.get("range", {}).get("start", {}).get("line", 0)
        message = " ".join(str(d.get("message", "")).split("\n\n")[0].split())[:MAX_MESSAGE]  # notes follow a blank line
        out.append(f"{lines.rel(path)}:{line + 1}: " + (f"{severity}: " if len(severities) > 1 else "") + message)
    return out


async def lsp_diagnostics(ctx: RunContext[Deps], path: str | None = None) -> str:
    """List errors and warnings from the language server: for `path`, or for every file a server has open."""
    await _authorize(ctx, "lsp_diagnostics", {"path": path or ""}, f"errors in {path or 'open files'}")
    lsp = ctx.deps.lsp
    assert lsp is not None
    lines = _Lines(ctx.deps)
    out: list[str] = []
    try:
        if path:
            p = _resolve(ctx.deps, path)
            client = await _client(ctx, p)
            uri = path_uri(p)
            mark = await lsp.sync(client, p)
            if mark is not None or uri not in client.published:
                await client.wait_diagnostics(uri, mark or 0, lsp.wait)
            out = _diagnostic_lines(lines, p, client.diagnostics.get(uri, []))
            if note := blocked_note(client.name, lines.rel(p), client.diagnostics.get(uri, [])):
                out.append(note)
        else:
            clients = [c for name in lsp.servers if (c := lsp.running(name))]
            if not clients:
                return "No language server is running. Pass `path` to check a file."
            for client in clients:
                await lsp.sync_open(client)
                for uri, diagnostics in sorted(client.diagnostics.items()):
                    if (f := uri_path(uri)) is not None and (f == ctx.deps.workspace or ctx.deps.workspace in f.parents):
                        out += _diagnostic_lines(lines, f, diagnostics)
                        if note := blocked_note(client.name, lines.rel(f), diagnostics):
                            out.append(note)
    except (LspUnavailable, LspError) as e:
        return str(e)
    if not out:
        return f"No errors or warnings in {path or 'the open files'}."
    return "\n".join(out[:MAX_DIAGNOSTICS]) + (f"\n[{len(out) - MAX_DIAGNOSTICS} more]" if len(out) > MAX_DIAGNOSTICS else "")


def _error_key(d: dict) -> tuple:
    return d.get("message"), str(d.get("code"))


def _error_keys(diagnostics: list[dict]) -> set[tuple]:
    return {_error_key(d) for d in diagnostics if d.get("severity", 1) == 1}


async def errors_after_edit(ctx: RunContext[Deps], changed: list[tuple[Path, str | None]],
                            renamed: tuple[str, str] | None = None) -> str:
    """Text to add to a write tool's result: errors a language server reports that weren't there before.

    `changed` holds each written file and its text before the write (None: it didn't exist). An error is new
    if no error with the same message and code was there before; after a rename, `renamed` (old, new) is
    applied to the old messages first. Never fails: a slow or missing server adds nothing."""
    lsp = ctx.deps.lsp
    if lsp is None or not lsp.diagnostics:
        return ""
    lines = _Lines(ctx.deps)
    # Send every file before waiting for any: a server checks a file against the others' latest contents.
    pending: list[tuple[Path, LspClient, set, int]] = []
    for p, before_text in changed:
        if (server := lsp.server_for(p)) is None:
            continue
        try:
            client = await lsp.client_for(server, ctx.deps, ctx.tool_call_id)
            uri = path_uri(p)
            if uri not in client.docs and before_text is not None:  # learn which errors were there already
                mark = await lsp.sync(client, p, before_text)
                await client.wait_diagnostics(uri, mark or 0, lsp.wait)
            before = _error_keys(client.diagnostics.get(uri, [])) if uri in client.docs else set()
            if renamed:
                before = {(re.sub(rf"\b{re.escape(renamed[0])}\b", renamed[1], str(m)), c) for m, c in before}
            if (mark := await lsp.sync(client, p)) is not None:
                pending.append((p, client, before, mark))
        except (LspUnavailable, LspError, OSError):
            continue
    new: dict[str, list[str]] = {}  # server -> error lines
    notes: list[str] = []  # files the server stopped checking: a clean result there means nothing
    old_count = 0
    for p, client, before, mark in pending:
        uri = path_uri(p)
        if not await client.wait_diagnostics(uri, mark, lsp.wait):
            continue
        if _error_keys(client.diagnostics.get(uri, [])) - before:
            # the first answer can come before the server has seen the other changed files
            await client.wait_diagnostics(uri, client.published.get(uri, 0), SETTLE)
        errors = [d for d in client.diagnostics.get(uri, []) if d.get("severity", 1) == 1]
        fresh = [d for d in errors if _error_key(d) not in before]
        old_count += len(errors) - len(fresh)
        new.setdefault(client.name, []).extend(_diagnostic_lines(lines, p, fresh, {1: "error"}))
        if note := blocked_note(client.name, lines.rel(p), client.diagnostics.get(uri, [])):
            notes.append(note)
    if not any(new.values()) and not notes:
        return ""
    out, shown = [], 0
    for name, found in new.items():
        if found and shown < MAX_NEW_ERRORS:
            out += [f"Errors after this edit ({name}):", *found[:MAX_NEW_ERRORS - shown]]
            shown += len(found[:MAX_NEW_ERRORS - shown])
    if (total := sum(map(len, new.values()))) > shown:
        out.append(f"[{total - shown} more]")
    if old_count and shown:
        out.append(f"({old_count} error{' was' if old_count == 1 else 's were'} there before this edit)")
    return "\n\n" + "\n".join(out + notes)


def apply_edits(text: str, edits: list[dict]) -> str:
    """`text` with LSP TextEdits applied. Ranges are in UTF-16 columns and refer to the original text."""
    lines = text.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def offset(pos: dict) -> int:
        n = pos.get("line", 0)
        if n >= len(lines):
            return len(text)
        return starts[n] + str_index(lines[n].rstrip("\r\n"), pos.get("character", 0))

    spans = sorted(((offset(e["range"]["start"]), offset(e["range"]["end"]), e.get("newText", "")) for e in edits),
                   reverse=True)
    for start, end, new in spans:
        text = text[:start] + new + text[end:]
    return text


def _file_edits(edit: dict) -> dict[str, list[dict]] | str:
    """URI -> text edits in a WorkspaceEdit, or why jimbo can't apply it."""
    files: dict[str, list[dict]] = {}
    for change in edit.get("documentChanges") or []:
        if "kind" in change:  # create, rename or delete a file
            return f"the rename needs a file to be {change['kind']}d, which jimbo doesn't do; use edit_file"
        files.setdefault(change["textDocument"]["uri"], []).extend(change.get("edits", []))
    if not files:
        for uri, edits in (edit.get("changes") or {}).items():
            files.setdefault(uri, []).extend(edits)
    return files


async def lsp_rename(ctx: RunContext[Deps], path: str, line: int, symbol: str, new_name: str) -> str:
    """Rename a symbol everywhere it is used, using the language server. The user approves one diff of every
    file changed.

    `line` is 1-based, as read_file shows it, and `symbol` is the name as written on that line."""
    from .fs import _diff  # fs imports this module for errors_after_edit

    deps = ctx.deps
    if not new_name.strip():
        raise ModelRetry("`new_name` is empty")
    p = _resolve(deps, path)
    pos = _position(p, path, line, symbol)
    try:
        client = await _client(ctx, p)
        await deps.lsp.sync_open(client)
        await deps.lsp.sync(client, p)
        edit = await _ask(ctx, client, "textDocument/rename",
                          {"textDocument": {"uri": path_uri(p)}, "position": pos, "newName": new_name})
    except (LspUnavailable, LspError) as e:
        return str(e)
    files = _file_edits(edit or {})
    if isinstance(files, str):
        return f"Nothing was changed: {files}."
    lines = _Lines(deps)
    changes: dict[Path, tuple[str, str, int]] = {}
    for uri, edits in files.items():
        f = uri_path(uri)
        try:
            target = resolve_in_workspace(deps.workspace, str(f)) if f else None
        except PathEscape:
            target = None
        if target is None:
            return f"Nothing was changed: the rename would change {f or uri}, which is outside the workspace."
        try:
            old = target.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return f"Nothing was changed: can't read {lines.rel(target)} ({e})."
        if (updated := apply_edits(old, edits)) != old:
            changes[target] = (old, updated, len(edits))
    if not changes:
        return f"The language server found nothing to rename for {symbol}."
    detail = "\n".join(_diff(lines.rel(f), old, new) for f, (old, new, _) in changes.items())
    try:
        await deps.authorize(ctx.tool_call_id, "lsp_rename", {"path": path},
                             f"rename {symbol} to {new_name} in {len(changes)} file(s)", detail=detail)
    except ToolDenied as e:
        raise ModelRetry(str(e)) from e
    for f, (_, new, _) in changes.items():
        deps.checkpoints.record(f)
        f.write_text(new, "utf-8")
    edits = sum(n for _, _, n in changes.values())
    report = "\n".join([f"renamed {symbol} to {new_name}: {edits} edit(s) in {len(changes)} file(s):",
                        *(f"  {lines.rel(f)} ({n})" for f, (_, _, n) in changes.items())])
    return report + await errors_after_edit(ctx, [(f, old) for f, (old, _, _) in changes.items()],
                                            (QUALIFIED.split(symbol)[-1], new_name))


async def _only_with_servers(ctx: RunContext[Deps], tool_def: ToolDefinition) -> ToolDefinition | None:
    return tool_def if ctx.deps.can_use_lsp else None


LSP_READ_TOOLS = [Tool(t, prepare=_only_with_servers)
                  for t in (lsp_definition, lsp_references, lsp_symbols, lsp_diagnostics)]
LSP_TOOLS = [*LSP_READ_TOOLS, Tool(lsp_rename, prepare=_only_with_servers)]

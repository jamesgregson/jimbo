"""A fake language server for tests: stdlib only, run as a script by LspClient.

It reads `.fk` files: `def name` defines a function, `class Name` a class (indented defs are its methods),
and a line containing `ERROR: text`, `WARN: text` or `BLOCK: text` (an error with code `blocked`) has that
diagnostic. Names are words. Environment:
FAKE_LSP_HANG=<method> never answers that method; FAKE_LSP_CRASH=<method> exits when asked it;
FAKE_LSP_SILENT=1 never publishes diagnostics.
"""

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

WORD = re.compile(r"\w+")
docs: dict[str, str] = {}
root = Path(".")
config: list = []


def send(message: dict) -> None:
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read() -> dict | None:
    length = None
    while (line := sys.stdin.buffer.readline()) not in (b"\r\n", b"\n"):
        if not line:
            return None
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))


def path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def text(uri: str) -> str:
    return docs[uri] if uri in docs else path(uri).read_text()


def all_files() -> dict[str, str]:
    files = {p.resolve().as_uri(): p.read_text() for p in root.rglob("*.fk")}
    return {**files, **docs}


def word_at(uri: str, pos: dict) -> str:
    line = text(uri).splitlines()[pos["line"]]
    for m in WORD.finditer(line):
        if m.start() <= pos["character"] <= m.end():
            return m.group()
    return ""


def rng(line: int, start: int, end: int) -> dict:
    return {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}


def occurrences(word: str) -> list[dict]:
    out = []
    for uri, body in sorted(all_files().items()):
        for n, line in enumerate(body.splitlines()):
            for m in re.finditer(rf"\b{re.escape(word)}\b", line):
                out.append({"uri": uri, "range": rng(n, m.start(), m.end())})
    return out


def definitions(word: str) -> list[dict]:
    return [loc for loc in occurrences(word)
            if re.search(rf"\b(def|class) {re.escape(word)}\b", text(loc["uri"]).splitlines()[loc["range"]["start"]["line"]])]


def publish(uri: str, version: int | None) -> None:
    if os.environ.get("FAKE_LSP_SILENT"):
        return
    diags = []
    for n, line in enumerate(docs[uri].splitlines()):
        if m := re.search(r"(ERROR|WARN|BLOCK): (.*)", line):
            diags.append({"range": rng(n, 0, len(line)), "severity": 2 if m.group(1) == "WARN" else 1,
                          "message": m.group(2).strip(), "code": "blocked" if m.group(1) == "BLOCK" else "x"})
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
          "params": {"uri": uri, "version": version, "diagnostics": diags}})


def symbols(uri: str) -> list[dict]:
    out: list[dict] = []
    for n, line in enumerate(text(uri).splitlines()):
        if m := re.match(r"(\s*)(def|class) (\w+)", line):
            s = {"name": m.group(3), "kind": 12 if m.group(2) == "def" else 5, "range": rng(n, 0, len(line)),
                 "selectionRange": rng(n, m.start(3), m.end(3)), "children": []}
            (out[-1]["children"] if m.group(1) and out else out).append(s)
    return out


def answer(method: str, params: dict):
    global root
    if method == "initialize":
        root = path(params["rootUri"])
        return {"capabilities": {"definitionProvider": True, "referencesProvider": True, "hoverProvider": True,
                                 "renameProvider": True, "documentSymbolProvider": True,
                                 "workspaceSymbolProvider": True, "textDocumentSync": 1}}
    if method == "shutdown":
        return None
    doc = params.get("textDocument", {}).get("uri")
    if method == "textDocument/definition":
        return definitions(word_at(doc, params["position"]))
    if method == "textDocument/references":
        return occurrences(word_at(doc, params["position"]))
    if method == "textDocument/hover":
        word = word_at(doc, params["position"])
        return {"contents": {"kind": "markdown", "value": f"`{word}`: a fake symbol; config {json.dumps(config)}"}}
    if method == "textDocument/documentSymbol":
        return symbols(doc)
    if method == "workspace/symbol":
        found = [loc for loc in occurrences(params["query"]) if loc in definitions(params["query"])]
        return [{"name": params["query"], "kind": 12, "location": loc} for loc in found]
    if method == "textDocument/rename":
        changes: dict[str, list] = {}
        for loc in occurrences(word_at(doc, params["position"])):
            changes.setdefault(loc["uri"], []).append({"range": loc["range"], "newText": params["newName"]})
        return {"changes": changes}
    raise KeyError(method)


def main() -> None:
    while (message := read()) is not None:
        method, params = message.get("method"), message.get("params") or {}
        if method is None:  # an answer to our workspace/configuration request
            config.extend(message.get("result") or [])
            continue
        if method == os.environ.get("FAKE_LSP_CRASH"):
            os._exit(1)
        if method == "initialized":
            send({"jsonrpc": "2.0", "id": "cfg", "method": "workspace/configuration",
                  "params": {"items": [{"section": "fake.option"}]}})
        elif method == "exit":
            return
        elif method == "textDocument/didOpen":
            d = params["textDocument"]
            docs[d["uri"]] = d["text"]
            publish(d["uri"], d["version"])
        elif method == "textDocument/didChange":
            d = params["textDocument"]
            docs[d["uri"]] = params["contentChanges"][-1]["text"]
            publish(d["uri"], d["version"])
        if "id" not in message or method == os.environ.get("FAKE_LSP_HANG"):
            continue
        try:
            send({"jsonrpc": "2.0", "id": message["id"], "result": answer(method, params)})
        except KeyError:
            send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"no {method}"}})


if __name__ == "__main__":
    main()

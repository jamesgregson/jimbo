"""Which language server handles which files: the built-in table, plus servers defined in config."""

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import ServerConfig, Settings


@dataclass(frozen=True)
class Language:
    label: str  # shown to the user, e.g. "C, C++"
    ids: dict[str, str]  # extension -> languageId sent in didOpen
    servers: tuple[str, ...]  # tried in order; the first on PATH is used


LANGUAGES = [
    Language("Python", {".py": "python", ".pyi": "python"}, ("basedpyright", "pyright", "pylsp")),
    Language("C, C++", {".c": "c", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hh": "cpp",
                        ".hpp": "cpp", ".hxx": "cpp"}, ("clangd",)),
    Language("Rust", {".rs": "rust"}, ("rust-analyzer",)),
    Language("Go", {".go": "go"}, ("gopls",)),
    Language("TypeScript, JavaScript", {".ts": "typescript", ".tsx": "typescriptreact", ".js": "javascript",
                                        ".jsx": "javascriptreact", ".mjs": "javascript", ".cjs": "javascript"},
             ("typescript-language-server",)),
]

COMMANDS = {
    "basedpyright": ["basedpyright-langserver", "--stdio"],
    "pyright": ["pyright-langserver", "--stdio"],
    "pylsp": ["pylsp"],
    "clangd": ["clangd"],
    "rust-analyzer": ["rust-analyzer"],
    "gopls": ["gopls"],
    "typescript-language-server": ["typescript-language-server", "--stdio"],
}

# Settings sent when the config doesn't set any. A project's own config file (pyproject.toml) still wins.
SETTINGS = {
    # basedpyright's default mode reports most untyped code; "standard" is pyright's default
    "basedpyright": {"basedpyright": {"analysis": {"typeCheckingMode": "standard"}}},
}

# Files in the workspace root that a server reads only when it starts. When one changes, jimbo restarts it.
RESTART_ON = {"clangd": ("compile_commands.json", "compile_flags.txt", ".clangd")}

# Diagnostic codes after which a server doesn't check the rest of a file, and what to tell the model.
BLOCKING = {"clangd": ({"pp_file_not_found"}, (
    "clangd can't find a header in {file}, so it doesn't check the rest of that file. Add the include paths to "
    "compile_flags.txt in the project root, one flag per line (-I/path/to/include), or generate "
    "compile_commands.json. jimbo restarts clangd when either file changes."))}


@dataclass(frozen=True)
class Server:
    name: str
    language: str  # label, as in Language
    command: tuple[str, ...]
    ids: dict[str, str]  # extension -> languageId
    env: dict[str, str] = field(default_factory=dict)
    init_options: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Choice:
    """One row of /lsp: a language and the server jimbo uses for it, or None and the names it tried."""
    language: str
    server: Server | None
    tried: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()


def _builtin_ids(name: str) -> dict[str, str]:
    return next((lang.ids for lang in LANGUAGES if name in lang.servers), {})


def _from_config(name: str, cfg: ServerConfig) -> Server | None:
    ids = _builtin_ids(name)
    if cfg.extensions is not None:
        ids = {e.lower(): cfg.language_id or ids.get(e.lower()) or e.lower().lstrip(".") for e in cfg.extensions}
    elif cfg.language_id:
        ids = dict.fromkeys(ids, cfg.language_id)
    command = cfg.command or COMMANDS.get(name)
    if not ids or not command:
        return None
    label = next((lang.label for lang in LANGUAGES if name in lang.servers), None) \
        or ", ".join(sorted(set(ids.values())))
    return Server(name, label, tuple(command), ids, dict(cfg.env), dict(cfg.init_options),
                  dict(cfg.settings or SETTINGS.get(name, {})))


def executables(settings: Settings) -> list[str]:
    """Every program resolve_servers may look for."""
    names = [c[0] for c in COMMANDS.values()]
    names += [cfg.command[0] for cfg in settings.lsp_servers.values() if cfg.command]
    return list(dict.fromkeys(names))


def resolve_servers(settings: Settings, which: Callable[[str], str | None] = shutil.which) -> tuple[
        list[Choice], list[str]]:
    """The server for each language, and warnings about servers in config that can't be used.

    A server in `lsp_servers` takes its extensions from the built-in languages, and a built-in server of the
    same name is replaced by it. A server is used only if its command is found."""
    disabled = set(settings.lsp_disabled)
    choices, warnings, claimed = [], [], set()
    for name, cfg in settings.lsp_servers.items():
        if name in disabled:
            continue
        server = _from_config(name, cfg)
        if server is None:
            warnings.append(f"lsp_servers.{name}: set command and extensions (it isn't a built-in server)")
            continue
        claimed |= server.ids.keys()
        choices.append(Choice(server.language, server if which(server.command[0]) else None, (name,),
                              extensions=tuple(server.ids)))
    for lang in LANGUAGES:
        if lang.ids.keys() <= claimed:
            continue
        names = [n for n in lang.servers if n not in disabled and n not in settings.lsp_servers]
        found = next((n for n in names if which(COMMANDS[n][0])), None)
        server = Server(found, lang.label, tuple(COMMANDS[found]),
                        {e: i for e, i in lang.ids.items() if e not in claimed},
                        settings=SETTINGS.get(found, {})) if found else None
        choices.append(Choice(lang.label, server, tuple(names), tuple(n for n in lang.servers if n in disabled),
                              tuple(e for e in lang.ids if e not in claimed)))
    return choices, warnings

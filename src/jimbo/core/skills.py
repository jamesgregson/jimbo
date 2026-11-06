"""Project instructions (AGENTS.md / JIMBO.md) and skills (`<dir>/<name>/SKILL.md`), loaded from disk."""

from dataclasses import dataclass, field
from pathlib import Path

from .commands import NAME_RE, parse_frontmatter

INSTRUCTION_FILES = ("AGENTS.md", "JIMBO.md")
MAX_INSTRUCTION_CHARS = 20_000
MEMORY_FILE = "memory.md"  # notes the user asked jimbo to remember; see the remember tool


def memory_paths(user_dir: Path, workspace: Path) -> dict[str, Path]:
    """Where `remember` saves notes: for all projects, and for this one."""
    return {"user": user_dir / MEMORY_FILE, "project": workspace / ".jimbo" / MEMORY_FILE}


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    models: dict[str, str] = field(default_factory=dict)  # task -> model alias, applied while the skill is in use
    interactive: bool = True  # false: no questions to the user (ask_user) while the skill is in use


def _read(path: Path) -> str | None:
    try:
        return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_project_instructions(user_dir: Path, workspace: Path) -> str:
    """Concatenate ~/.jimbo/AGENTS.md, the workspace's AGENTS.md / JIMBO.md, then the user and project memory
    files (each capped)."""
    memory = memory_paths(user_dir, workspace)
    files = [*((user_dir / f, f"user instructions ({f})") for f in INSTRUCTION_FILES),
             *((workspace / f, f"project instructions ({f})") for f in INSTRUCTION_FILES),
             (memory["user"], "user memory (~/.jimbo/memory.md): notes the user asked you to remember"),
             (memory["project"], "project memory (.jimbo/memory.md): notes the user asked you to remember")]
    parts = []
    for path, header in files:
        text = _read(path)
        if text and text.strip():
            text = text.strip()
            if len(text) > MAX_INSTRUCTION_CHARS:
                text = text[:MAX_INSTRUCTION_CHARS] + "\n[truncated]"
            parts.append(f"# {header}\n{text}")
    return "\n\n".join(parts)


def load_skills(dirs: list[Path]) -> dict[str, Skill]:
    """Discover `<dir>/*/SKILL.md`. Later dirs override earlier ones."""
    skills: dict[str, Skill] = {}
    for d in dirs:
        for path in sorted(d.glob("*/SKILL.md")) if d.is_dir() else []:
            text = _read(path)
            if text is None:
                continue
            meta, body = parse_frontmatter(text)
            name = str(meta.get("name") or path.parent.name)
            if not NAME_RE.fullmatch(name):
                continue
            raw = meta.get("models")
            models = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
            interactive = meta.get("interactive", True) not in (False, "false")
            skills[name] = Skill(name, " ".join(str(meta.get("description") or "").split()), body, path, models,
                                 interactive)
    return skills


def format_skill_index(skills: dict[str, Skill]) -> str:
    lines = [f"- {s.name}: {s.description}" if s.description else f"- {s.name}" for s in skills.values()]
    return "\n".join(lines)


async def reload_and_describe(deps) -> str:
    """Reread every skill file now (for the reload_skills tool and /reload), and say which skills changed."""
    def snapshot() -> dict[str, tuple]:
        return {n: (s.description, s.body, s.interactive, s.models) for n, s in deps.skills.items()}

    before = snapshot()
    await deps.reload_skills(force=True)
    after = snapshot()
    changes = [(label, sorted(names)) for label, names in (
        ("added", after.keys() - before.keys()), ("removed", before.keys() - after.keys()),
        ("changed", {n for n in after.keys() & before.keys() if after[n] != before[n]}))]
    found = "; ".join(f"{label}: {', '.join(names)}" for label, names in changes if names)
    return f"Skills reloaded ({len(after)}). {found or 'No skill changed.'}"


def use_skill(deps, skill: Skill) -> str | None:
    """Apply the skill's frontmatter for the rest of this turn: `interactive: false` turns off questions to the
    user, and `models:` choices are scoped to the turn. Returns a warning if the models can't be applied (unknown
    alias): the skill still runs, on the models the user configured."""
    if not skill.interactive:
        deps.no_questions = skill.name
    if not skill.models or deps.models is None:
        return None
    try:
        deps.models.push_scope(f"skill {skill.name}", skill.models)
    except ValueError as e:
        return str(e)
    return None

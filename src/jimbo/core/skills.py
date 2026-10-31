"""Project instructions (AGENTS.md / JIMBO.md) and skills (`<dir>/<name>/SKILL.md`), loaded from disk."""

from dataclasses import dataclass
from pathlib import Path

from .commands import NAME_RE, parse_frontmatter

INSTRUCTION_FILES = ("AGENTS.md", "JIMBO.md")
MAX_INSTRUCTION_CHARS = 20_000


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path


def _read(path: Path) -> str | None:
    try:
        return path.read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_project_instructions(user_dir: Path, workspace: Path) -> str:
    """Concatenate ~/.jimbo/AGENTS.md and the workspace's AGENTS.md / JIMBO.md (each capped)."""
    parts = []
    for base, label in ((user_dir, "user"), (workspace, "project")):
        for fname in INSTRUCTION_FILES:
            text = _read(base / fname)
            if text and text.strip():
                text = text.strip()
                if len(text) > MAX_INSTRUCTION_CHARS:
                    text = text[:MAX_INSTRUCTION_CHARS] + "\n[truncated]"
                parts.append(f"# {label} instructions ({fname})\n{text}")
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
            skills[name] = Skill(name, " ".join(str(meta.get("description") or "").split()), body, path)
    return skills


def format_skill_index(skills: dict[str, Skill]) -> str:
    lines = [f"- {s.name}: {s.description}" if s.description else f"- {s.name}" for s in skills.values()]
    return "\n".join(lines)

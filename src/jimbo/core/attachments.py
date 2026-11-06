"""User input beyond plain text: pasted images and `@path` file mentions."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.messages import BinaryContent, UserContent

from ..tools.paths import PathEscape, resolve_in_workspace

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_SUFFIXES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp"}
MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGES = 4
MAX_MENTION_BYTES = 64_000
# "@path" at a word start; trailing sentence punctuation is not part of the path.
MENTION_RE = re.compile(r"(?<![\w@])@([\w./~+-][\w./~+-]*[\w/~+-])")


@dataclass
class UserMessage:
    """What a frontend returns when the input carries more than text."""

    text: str
    images: list[BinaryContent] = field(default_factory=list)


def check_image(media_type: str, data: bytes) -> str | None:
    """Why an image can't be attached, or None if it is fine."""
    if media_type not in IMAGE_TYPES:
        return f"unsupported image type {media_type!r}"
    if len(data) > MAX_IMAGE_BYTES:
        return f"image is {len(data) // 1000} KB; the limit is {MAX_IMAGE_BYTES // 1000} KB"
    return None


def mentioned_images(text: str, workspace: Path) -> tuple[list[BinaryContent], list[dict], list[str]]:
    """Image files mentioned as `@path` (by extension), to attach like pasted images. Returns the images, their
    metadata ({kind, path, media_type, bytes}) and why any couldn't be attached."""
    images, meta, problems, seen = [], [], [], set()
    for m in MENTION_RE.finditer(text):
        rel = m.group(1)
        media_type = IMAGE_SUFFIXES.get(Path(rel).suffix.lower())
        if media_type is None or rel in seen:
            continue
        seen.add(rel)
        try:
            p = resolve_in_workspace(workspace, rel)
            if not p.is_file():
                continue
            data = p.read_bytes()
        except (PathEscape, OSError):
            continue
        if err := check_image(media_type, data):
            problems.append(f"@{rel}: {err}")
            continue
        images.append(BinaryContent(data, media_type=media_type))
        meta.append({"kind": "image", "path": rel, "media_type": media_type, "bytes": len(data)})
    return images, meta, problems


def expand_mentions(text: str, workspace: Path) -> tuple[str, list[dict]]:
    """Append the contents of workspace files mentioned as `@path`; unknown mentions are left alone.

    Returns the new text and metadata ({path, bytes, truncated}) for each inlined file.
    """
    blocks, meta, seen = [], [], set()
    for m in MENTION_RE.finditer(text):
        rel = m.group(1)
        if rel in seen or Path(rel).suffix.lower() in IMAGE_SUFFIXES:  # images: see mentioned_images
            continue
        try:
            p = resolve_in_workspace(workspace, rel)
            if not p.is_file():
                continue
            raw = p.read_bytes()[:MAX_MENTION_BYTES + 1]
            body = raw[:MAX_MENTION_BYTES].decode("utf-8", "strict" if len(raw) <= MAX_MENTION_BYTES else "ignore")
        except (PathEscape, OSError, UnicodeDecodeError):
            continue
        seen.add(rel)
        truncated = len(raw) > MAX_MENTION_BYTES
        blocks.append(f'<file path="{rel}">\n{body}{chr(10) + "[truncated]" if truncated else ""}\n</file>')
        meta.append({"path": rel, "bytes": len(raw), "truncated": truncated})
    if not blocks:
        return text, []
    return text + "\n\n" + "\n".join(blocks), meta


def content_text(content: object) -> str:
    """Tool output as text for events and transcripts: images become `[image]`, never their bytes."""
    if isinstance(content, BinaryContent):
        return f"[image {content.media_type}, {len(content.data)} bytes]"
    if isinstance(content, list):
        return "\n".join(content_text(c) for c in content)
    return str(content)


def image_meta(images: list[BinaryContent]) -> list[dict]:
    return [{"kind": "image", "media_type": i.media_type, "bytes": len(i.data)} for i in images]


def build_content(text: str, images: list[BinaryContent]) -> str | list[UserContent]:
    return ([text] if text.strip() else []) + images if images else text

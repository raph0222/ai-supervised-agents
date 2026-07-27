"""Chunk seed/knowledge/*.md for the vector store.

Splits on `##` headings rather than a fixed token count: the policy documents
are one rule per section, so a heading boundary is also a semantic one and a
threshold never gets cut away from the sentence qualifying it.

Frontmatter becomes chunk metadata — `authority` in particular, so retrieval can
rank a binding policy above an informational FAQ. It is parsed by hand; the
shape is fixed and ours, so there is no YAML dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# A section longer than this is split further, at paragraph boundaries, with the
# heading repeated so every chunk still says which rule it belongs to.
MAX_CHUNK_CHARS = 1400
MIN_CHUNK_CHARS = 80

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LIST = re.compile(r"\A\[(.*)\]\Z")


@dataclass
class Chunk:
    source_file: str
    chunk_index: int
    content: str
    heading: str | None
    policy_id: str
    title: str
    category: str
    authority: str
    version: str
    applies_to: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (metadata, body). Missing frontmatter yields an empty dict."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    meta: dict = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip().strip('"').strip("'")
        if (as_list := _LIST.match(value)) is not None:
            meta[key.strip()] = [
                item.strip().strip('"').strip("'")
                for item in as_list.group(1).split(",")
                if item.strip()
            ]
        else:
            meta[key.strip()] = value
    return meta, text[match.end():]


def chunk_markdown(path: Path, *, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)

    common = {
        "source_file": path.name,
        "policy_id": str(meta.get("policy_id", path.stem.upper())),
        "title": str(meta.get("title", path.stem.replace("-", " ").title())),
        "category": str(meta.get("category", "general")),
        "authority": str(meta.get("authority", "informational")),
        "version": str(meta.get("version", "")),
        "applies_to": list(meta.get("applies_to", []) or []),
    }

    chunks: list[Chunk] = []
    for heading, section in _sections(body):
        for piece in _split_long(section, max_chars):
            text = piece.strip()
            if len(text) < MIN_CHUNK_CHARS:
                # Too short to retrieve usefully on its own — fold it into the
                # previous chunk rather than emitting a stub.
                if chunks:
                    chunks[-1].content += "\n\n" + text
                continue
            body_text = f"## {heading}\n\n{text}" if heading else text
            chunks.append(
                Chunk(
                    chunk_index=len(chunks),
                    content=f"[{common['policy_id']} — {common['title']}]\n{body_text}",
                    heading=heading,
                    **common,
                )
            )
    return chunks


def chunk_directory(knowledge_dir: Path) -> list[Chunk]:
    """Every .md in the directory, sorted so chunk ids are stable across runs."""
    out: list[Chunk] = []
    for path in sorted(Path(knowledge_dir).glob("*.md")):
        out.extend(chunk_markdown(path))
    return out


# ----------------------------------------------------------------------


def _sections(body: str) -> list[tuple[str | None, str]]:
    """Split on `##` headings. The `#` title and any preamble ride along with
    the first section rather than becoming a chunk of their own."""
    sections: list[tuple[str | None, str]] = []
    heading: str | None = None
    buffer: list[str] = []

    for line in body.splitlines():
        if line.startswith("## "):
            if buffer:
                sections.append((heading, "\n".join(buffer)))
            heading = line[3:].strip()
            buffer = []
        elif line.startswith("# "):
            continue  # document title; already in the metadata
        else:
            buffer.append(line)

    if buffer:
        sections.append((heading, "\n".join(buffer)))
    return [(h, s) for h, s in sections if s.strip()]


def _split_long(section: str, max_chars: int) -> list[str]:
    if len(section) <= max_chars:
        return [section]

    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for para in section.split("\n\n"):
        if size and size + len(para) > max_chars:
            pieces.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para) + 2
    if current:
        pieces.append("\n\n".join(current))
    return pieces

"""Load, embed and retrieve the knowledge corpus.

`seed/knowledge/*.md` is the source of truth; these rows are a rebuildable index
of it. Loading upserts on (source_file, chunk_index), so editing one paragraph
re-embeds only that paragraph.

Chunks are stored whether or not Vertex is configured — without credentials only
the `embedding` column stays NULL.

Chunks are stored as *templates*: the prose holds `{{policy_rule}}` placeholders
that `rag.template` resolves against the live `policy_rules` rows on every read,
so retrieved text can never quote a threshold the engine no longer enforces.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import models as m
from app.rag import embeddings, template
from app.rag.chunker import chunk_directory

log = logging.getLogger(__name__)

# A binding policy outranks an informational FAQ when both are close.
# Small enough that it only breaks near-ties, never overturns a clear winner.
AUTHORITY_BOOST = 0.05


@dataclass
class LoadResult:
    files: int
    chunks: int
    inserted: int
    updated: int
    deleted: int
    unchanged: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def knowledge_dir(seed_dir: Path | None = None) -> Path:
    return Path(seed_dir or get_settings().seed_dir) / "knowledge"


# ----------------------------------------------------------------------
# load
# ----------------------------------------------------------------------


def load_corpus(session: Session, seed_dir: Path | None = None) -> LoadResult:
    """Chunk every markdown file into `knowledge_chunks`. Idempotent.

    Changed content clears the stored embedding so the next embed pass picks it
    up, rather than leaving a stale vector on edited text.
    """
    directory = knowledge_dir(seed_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"knowledge directory not found: {directory}")

    chunks = chunk_directory(directory)
    existing = {
        (row.source_file, row.chunk_index): row
        for row in session.scalars(select(m.KnowledgeChunk)).all()
    }

    inserted = updated = unchanged = 0
    seen: set[tuple[str, int]] = set()

    for chunk in chunks:
        key = (chunk.source_file, chunk.chunk_index)
        seen.add(key)
        row = existing.get(key)
        fields = dict(
            policy_id=chunk.policy_id,
            title=chunk.title,
            category=chunk.category,
            authority=chunk.authority,
            applies_to=chunk.applies_to,
            version=chunk.version,
            heading=chunk.heading,
            content=chunk.content,
        )
        if row is None:
            session.add(
                m.KnowledgeChunk(
                    source_file=chunk.source_file,
                    chunk_index=chunk.chunk_index,
                    embedding=None,
                    **fields,
                )
            )
            inserted += 1
            continue

        content_changed = row.content != chunk.content
        if content_changed or any(getattr(row, k) != v for k, v in fields.items()):
            for k, v in fields.items():
                setattr(row, k, v)
            if content_changed:
                row.embedding = None
            updated += 1
        else:
            unchanged += 1

    # Chunks whose source section was deleted or whose file shrank.
    deleted = 0
    for key, row in existing.items():
        if key not in seen:
            session.delete(row)
            deleted += 1

    session.flush()
    result = LoadResult(
        files=len({c.source_file for c in chunks}),
        chunks=len(chunks),
        inserted=inserted,
        updated=updated,
        deleted=deleted,
        unchanged=unchanged,
    )
    log.info("knowledge corpus loaded: %s", result.as_dict())
    return result


# ----------------------------------------------------------------------
# embed
# ----------------------------------------------------------------------


def embed_missing(session: Session, *, limit: int | None = None) -> int:
    """Embed every chunk with a NULL vector. Returns the number embedded.

    Raises MissingCredentials when Vertex is unconfigured — callers that want the
    documented degraded mode should check `embeddings.is_configured()` first
    rather than swallowing the error here.

    The *rendered* text is embedded, not the template: a vector built from
    `{{refund_auto_approve_under_cents|money}}` would not match a customer
    asking whether a $40 refund is automatic.
    """
    embeddings.require_configured()

    stmt = select(m.KnowledgeChunk).where(m.KnowledgeChunk.embedding.is_(None))
    if limit:
        stmt = stmt.limit(limit)
    pending = session.scalars(stmt).all()
    if not pending:
        return 0

    rules = template.values(session)
    vectors = embeddings.embed_documents(
        [template.render(c.content, rules) for c in pending]
    )
    for chunk, vector in zip(pending, vectors, strict=True):
        chunk.embedding = vector
    session.flush()
    log.info("embedded %d chunks", len(pending))
    return len(pending)


def invalidate_for_rule(session: Session, key: str) -> int:
    """Drop the embeddings of every chunk whose prose cites `key`.

    The text an agent reads is rendered at retrieval, so it is already correct
    the moment the rule row changes. The vectors are not: they encode the old
    number. NULLing them queues a re-embed rather than leaving a stale one.
    """
    rows = [
        row
        for row in session.scalars(
            select(m.KnowledgeChunk).where(m.KnowledgeChunk.embedding.is_not(None))
        ).all()
        if key in template.keys_in(row.content)
    ]
    for row in rows:
        row.embedding = None
    session.flush()
    if rows:
        log.info("invalidated %d embeddings citing %s", len(rows), key)
    return len(rows)


def clear_embeddings(session: Session) -> int:
    """Force a full re-embed on the next pass."""
    rows = session.scalars(
        select(m.KnowledgeChunk).where(m.KnowledgeChunk.embedding.is_not(None))
    ).all()
    for row in rows:
        row.embedding = None
    session.flush()
    return len(rows)


def stats(session: Session) -> dict:
    total = session.scalar(select(func.count()).select_from(m.KnowledgeChunk)) or 0
    embedded = (
        session.scalar(
            select(func.count())
            .select_from(m.KnowledgeChunk)
            .where(m.KnowledgeChunk.embedding.is_not(None))
        )
        or 0
    )
    by_policy = dict(
        session.execute(
            select(m.KnowledgeChunk.policy_id, func.count())
            .group_by(m.KnowledgeChunk.policy_id)
            .order_by(m.KnowledgeChunk.policy_id)
        ).all()
    )

    # Placeholder health: a typo in a policy document should surface on /admin,
    # not inside a customer-facing answer.
    rules = template.values(session)
    rows = session.scalars(select(m.KnowledgeChunk)).all()
    templated = sum(1 for row in rows if template.keys_in(row.content))
    dangling = sorted({
        placeholder
        for row in rows
        for placeholder in template.unresolved(row.content, rules)
    })

    return {
        "chunks": total,
        "embedded": embedded,
        "missing_embeddings": total - embedded,
        "by_policy": by_policy,
        "templated_chunks": templated,
        "unresolved_placeholders": dangling,
    }


# ----------------------------------------------------------------------
# retrieve
# ----------------------------------------------------------------------


@dataclass
class Hit:
    chunk_id: int
    policy_id: str
    title: str
    heading: str | None
    category: str
    authority: str
    source_file: str
    content: str
    score: float
    mode: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def search(
    session: Session,
    query: str,
    *,
    k: int = 5,
    category: str | None = None,
    applies_to: str | None = None,
) -> list[Hit]:
    """Top-k chunks for a question, with policy placeholders resolved.

    Vector search when the corpus is embedded, keyword fallback otherwise. The
    fallback labels itself in `mode` so it is never mistaken for the real thing.

    Rules are read once per search and handed down, so every Hit leaving here
    quotes the thresholds currently in `policy_rules` — this is the only exit
    from the corpus, which is why rendering belongs here and not in each caller.
    """
    rules = template.values(session)
    if _embedded_count(session) and embeddings.is_configured():
        try:
            return _vector_search(session, query, k, category, applies_to, rules)
        except embeddings.MissingCredentials:
            pass
    return _keyword_search(session, query, k, category, applies_to, rules)


def _vector_search(
    session: Session, query: str, k: int, category: str | None,
    applies_to: str | None, rules: dict[str, int],
) -> list[Hit]:
    vector = embeddings.embed_query(query)
    distance = m.KnowledgeChunk.embedding.cosine_distance(vector).label("distance")

    stmt = (
        select(m.KnowledgeChunk, distance)
        .where(m.KnowledgeChunk.embedding.is_not(None))
        .order_by(distance)
        # Over-fetch so the authority boost has candidates to reorder.
        .limit(max(k * 3, 10))
    )
    stmt = _filtered(session, stmt, category, applies_to)

    scored = [
        (row, (1.0 - dist) + (AUTHORITY_BOOST if row.authority == "binding" else 0.0))
        for row, dist in session.execute(stmt).all()
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [_hit(row, score, "vector", rules) for row, score in scored[:k]]


def _keyword_search(
    session: Session, query: str, k: int, category: str | None,
    applies_to: str | None, rules: dict[str, int],
) -> list[Hit]:
    terms = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 3}
    stmt = _filtered(session, select(m.KnowledgeChunk), category, applies_to)
    rows = session.scalars(stmt).all()

    scored = []
    for row in rows:
        # Match against rendered text so a question phrased with the current
        # threshold still hits the chunk that states it.
        haystack = f"{row.title} {row.heading or ''} {template.render(row.content, rules)}".lower()
        overlap = sum(1 for t in terms if t in haystack)
        if not overlap:
            continue
        score = overlap / max(len(terms), 1)
        if row.authority == "binding":
            score += AUTHORITY_BOOST
        scored.append((row, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return [_hit(row, score, "keyword-fallback", rules) for row, score in scored[:k]]


def _filtered(session: Session, stmt, category: str | None, applies_to: str | None):
    if category:
        stmt = stmt.where(m.KnowledgeChunk.category == category)
    if applies_to:
        # `applies_to` is JSON, not JSONB, so there is no containment operator.
        # The corpus is small enough to resolve the eligible ids in Python.
        allowed = [
            cid
            for cid, values in session.execute(
                select(m.KnowledgeChunk.id, m.KnowledgeChunk.applies_to)
            ).all()
            if applies_to in (values or [])
        ]
        stmt = stmt.where(m.KnowledgeChunk.id.in_(allowed))
    return stmt


def _embedded_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(m.KnowledgeChunk)
            .where(m.KnowledgeChunk.embedding.is_not(None))
        )
        or 0
    )


def _hit(
    row: m.KnowledgeChunk, score: float, mode: str, rules: dict[str, int]
) -> Hit:
    return Hit(
        chunk_id=row.id,
        policy_id=row.policy_id,
        title=row.title,
        heading=row.heading,
        category=row.category,
        authority=row.authority,
        source_file=row.source_file,
        content=template.render(row.content, rules),
        score=round(score, 4),
        mode=mode,
    )

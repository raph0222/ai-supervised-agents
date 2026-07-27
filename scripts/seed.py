#!/usr/bin/env python
"""Feed the database and the RAG corpus from seed/.

    python scripts/seed.py                  # schema + data (if empty) + knowledge
    python scripts/seed.py --reset          # truncate and reload everything
    python scripts/seed.py --knowledge-only # re-chunk / re-embed the corpus only
    python scripts/seed.py --data-only      # relational tables only
    python scripts/seed.py --no-embed       # load chunks, skip the Vertex calls
    python scripts/seed.py --re-embed       # drop stored vectors and embed again
    python scripts/seed.py --search "how long do I have to return a graphics card?"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

# Policy documents contain em dashes; a cp1252 console would mangle the snippets.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings  # noqa: E402
from app.db import models as m  # noqa: E402
from app.db.session import init_schema, session_scope  # noqa: E402
from app.rag import embeddings, store  # noqa: E402
from app.seed import seeder  # noqa: E402

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def step(text: str) -> None:
    print(f"\n{DIM}==>{RESET} {text}")


def ok(text: str) -> None:
    print(f"  {GREEN}OK{RESET}   {text}")


def warn(text: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {text}")


def fail(text: str) -> None:
    print(f"  {RED}FAIL{RESET} {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true",
                        help="truncate the seeded tables and reload (idempotent reset)")
    parser.add_argument("--data-only", action="store_true", help="skip the knowledge corpus")
    parser.add_argument("--knowledge-only", action="store_true", help="skip the relational seed")
    parser.add_argument("--no-embed", action="store_true", help="load chunks but do not call Vertex")
    parser.add_argument("--re-embed", action="store_true",
                        help="clear stored vectors first, then embed everything again")
    parser.add_argument("--seed-dir", type=Path, default=None, help="override seed/ location")
    parser.add_argument("--search", metavar="QUERY", default=None,
                        help="run a retrieval query after seeding, to prove the corpus works")
    args = parser.parse_args()

    settings = get_settings()
    seed_dir = args.seed_dir or settings.seed_dir

    print(f"{DIM}database : {_redact(settings.database_url)}{RESET}")
    print(f"{DIM}seed dir : {seed_dir}{RESET}")
    print(f"{DIM}vertex   : {'configured' if settings.vertex_configured else 'NOT configured'}{RESET}")

    step("Schema (pgvector extension + tables)")
    try:
        init_schema()
    except Exception as exc:  # noqa: BLE001
        fail(f"cannot reach the database: {exc}")
        print("\nIs Postgres up?  docker compose up -d db")
        return 1
    ok("schema ready")

    # --- relational ----------------------------------------------------
    if not args.knowledge_only:
        step("Relational seed (customer, products, orders, payments, tickets)")
        with session_scope() as session:
            if args.reset:
                counts = seeder.reseed(session, seed_dir)
                ok(f"reset and reloaded: {counts}")
            elif seeder.is_empty(session):
                counts = seeder.seed_all(session, seed_dir)
                ok(f"loaded: {counts}")
            else:
                seeder._rebuild_simulate_registry(seed_dir)
                warn("tables already populated - left alone (use --reset to reload)")

    # --- knowledge / RAG -----------------------------------------------
    if not args.data_only:
        step("Knowledge corpus (chunk -> embed -> pgvector)")
        with session_scope() as session:
            result = store.load_corpus(session, seed_dir)
            ok(
                f"{result.chunks} chunks from {result.files} files "
                f"(+{result.inserted} new, ~{result.updated} changed, "
                f"-{result.deleted} stale, {result.unchanged} unchanged)"
            )

            if args.re_embed:
                cleared = store.clear_embeddings(session)
                ok(f"cleared {cleared} stored vectors")

            if args.no_embed:
                warn("--no-embed: vectors left NULL")
            elif not embeddings.is_configured():
                warn(
                    f"{', '.join(settings.missing_vertex_vars())} not set - chunks stored "
                    "without embeddings."
                )
                print(f"       {DIM}Retrieval falls back to keyword matching until you "
                      f"set it and re-run this script.{RESET}")
            else:
                try:
                    n = store.embed_missing(session)
                    ok(f"embedded {n} chunk(s) with {settings.vertex_embedding_model}")
                except Exception as exc:  # noqa: BLE001
                    fail(f"embedding failed: {type(exc).__name__}: {exc}")
                    warn("chunks are stored; fix credentials and re-run with --knowledge-only")

    # --- summary --------------------------------------------------------
    step("Summary")
    with session_scope() as session:
        for model, label in (
            (m.Customer, "customers"),
            (m.Product, "products"),
            (m.InventoryVariant, "inventory variants"),
            (m.Order, "orders"),
            (m.Payment, "payments"),
            (m.Return, "returns"),
            (m.CrmTicket, "crm tickets"),
            (m.PolicyRule, "policy rules"),
        ):
            print(f"  {session.query(model).count():>4}  {label}")

        s = store.stats(session)
        print(f"  {s['chunks']:>4}  knowledge chunks ({s['embedded']} embedded, "
              f"{s['missing_embeddings']} without vectors)")
        for policy_id, count in s["by_policy"].items():
            print(f"          {DIM}{policy_id:<12} {count} chunks{RESET}")

        if args.search:
            step(f"Retrieval check: {args.search!r}")
            hits = store.search(session, args.search, k=3)
            if not hits:
                fail("no hits — the corpus is empty or the query matched nothing")
            for i, hit in enumerate(hits, 1):
                head = hit.heading or hit.title
                print(f"  {i}. [{hit.mode}] {hit.policy_id} / {head} "
                      f"({hit.authority}, score {hit.score})")
                snippet = " ".join(hit.content.split())[:160]
                print(f"     {DIM}{snippet}...{RESET}")

    print(f"\n{GREEN}Done.{RESET}\n")
    return 0


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}"


if __name__ == "__main__":
    raise SystemExit(main())

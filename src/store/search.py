#!/usr/bin/env python3
"""Search and inspect the corpus document store.

Searchable and sorted over hundreds of thousands of documents/pages via the
SQLite index (FTS5 for keyword search; metadata columns for sort/filter) — no
scanning of the underlying files.

    # keyword search over page text (FTS5)
    python src/store/search.py find "budget forecast"
    python src/store/search.py find "roadmap" --type pdf --sort date --limit 20

    # list documents, sortable
    python src/store/search.py docs --sort size
    python src/store/search.py docs --name "*review*"

    # show a document's version relationships (shared/added/removed pages)
    python src/store/search.py versions <document_id>
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "database"

SOURCE_NAMES = {1: "pdf", 2: "pptx"}
SOURCE_CODES = {v: k for k, v in SOURCE_NAMES.items()}
RELATION_NAMES = {1: "extends", 2: "shortened", 3: "reordered", 4: "modified"}

SORT_COLUMNS = {
    "date": "created_at",
    "size": "file_size",
    "pages": "page_count",
    "name": "source_name",
}


def _connect(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).exists():
        print(f"error: database not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def cmd_find(conn: sqlite3.Connection, args) -> int:
    """Full-text search over page text, joined back to their documents."""
    sort_col = SORT_COLUMNS.get(args.sort, "rank")
    order = "d.created_at DESC" if sort_col == "created_at" else \
            f"d.{sort_col} DESC" if sort_col in SORT_COLUMNS.values() else "f.rank"
    type_filter = ""
    params: list = [args.query]
    if args.type:
        if args.type not in SOURCE_CODES:
            print(f"error: --type must be one of {list(SOURCE_CODES)}", file=sys.stderr)
            return 1
        type_filter = "AND d.source_type = ?"
        params.append(SOURCE_CODES[args.type])
    params.append(args.limit)

    rows = conn.execute(
        f"""
        SELECT d.id, d.source_name, d.source_type, d.title, d.created_at,
               dp.position, p.hash,
               snippet(page_fts, 0, '[', ']', '...', 8) AS snip
        FROM page_fts f
        JOIN page p            ON p.hash = f.hash
        JOIN document_page dp  ON dp.page_hash = p.hash
        JOIN document d        ON d.id = dp.document_id
        WHERE page_fts MATCH ? {type_filter}
        ORDER BY {order}
        LIMIT ?
        """,
        params,
    ).fetchall()

    if not rows:
        print("no matches")
        return 2
    for r in rows:
        src = SOURCE_NAMES.get(r["source_type"], "?")
        name = r["source_name"] or r["title"] or f"doc:{r['id']}"
        print(f"[{src}] {name}  (doc {r['id']}, page {r['position']})")
        print(f"    {r['snip']}")
    return 0


def cmd_docs(conn: sqlite3.Connection, args) -> int:
    """List documents with sort/filter over metadata."""
    sort_col = SORT_COLUMNS.get(args.sort, "created_at")
    where, params = "", []
    if args.name:
        where = "WHERE source_name GLOB ?"
        params.append(args.name)
    if args.type:
        if args.type not in SOURCE_CODES:
            print(f"error: --type must be one of {list(SOURCE_CODES)}", file=sys.stderr)
            return 1
        where = where + (" AND " if where else "WHERE ") + "source_type = ?"
        params.append(SOURCE_CODES[args.type])
    params.append(args.limit)

    rows = conn.execute(
        f"SELECT id, source_name, source_type, page_count, file_size, created_at "
        f"FROM document {where} ORDER BY {sort_col} DESC LIMIT ?",
        params,
    ).fetchall()
    if not rows:
        print("no documents")
        return 2
    for r in rows:
        src = SOURCE_NAMES.get(r["source_type"], "?")
        print(f"doc {r['id']:>20}  [{src}]  {r['page_count']:>4}p  "
              f"{r['file_size']:>12}B  {r['created_at']}  {r['source_name'] or ''}")
    return 0


def cmd_versions(conn: sqlite3.Connection, args) -> int:
    """Show version relationships for a document (from document_edge)."""
    doc = conn.execute("SELECT * FROM document WHERE id = ?", (args.document_id,)).fetchone()
    if not doc:
        print(f"error: no document with id {args.document_id}", file=sys.stderr)
        return 1
    print(f"document {doc['id']}  {doc['source_name'] or ''}  ({doc['page_count']} pages)")

    parents = conn.execute(
        "SELECT parent_id, relation, added, removed FROM document_edge WHERE child_id = ?",
        (args.document_id,),
    ).fetchall()
    children = conn.execute(
        "SELECT child_id, relation, added, removed FROM document_edge WHERE parent_id = ?",
        (args.document_id,),
    ).fetchall()

    if parents:
        print("  derived from:")
        for e in parents:
            rel = RELATION_NAMES.get(e["relation"], "?")
            print(f"    <- doc {e['parent_id']}  ({rel}; +{e['added']} / -{e['removed']} pages)")
    if children:
        print("  has derivatives:")
        for e in children:
            rel = RELATION_NAMES.get(e["relation"], "?")
            print(f"    -> doc {e['child_id']}  ({rel}; +{e['added']} / -{e['removed']} pages)")
    if not parents and not children:
        print("  no recorded version relationships")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Search the corpus document store.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to the database file")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("find", help="full-text search over page/slide text")
    f.add_argument("query")
    f.add_argument("--type", choices=list(SOURCE_CODES))
    f.add_argument("--sort", default="rank", choices=["rank", *SORT_COLUMNS])
    f.add_argument("--limit", type=int, default=20)

    d = sub.add_parser("docs", help="list documents (sortable/filterable)")
    d.add_argument("--type", choices=list(SOURCE_CODES))
    d.add_argument("--name", help="GLOB filter on source_name, e.g. '*review*'")
    d.add_argument("--sort", default="date", choices=list(SORT_COLUMNS))
    d.add_argument("--limit", type=int, default=50)

    v = sub.add_parser("versions", help="show version relationships of a document")
    v.add_argument("document_id", type=int)

    args = p.parse_args(argv)
    conn = _connect(args.db)
    try:
        if args.cmd == "find":
            return cmd_find(conn, args)
        if args.cmd == "docs":
            return cmd_docs(conn, args)
        if args.cmd == "versions":
            return cmd_versions(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

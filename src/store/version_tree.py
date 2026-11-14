#!/usr/bin/env python3
"""Version-tree and space-savings queries over the corpus store.

Powers the website's version-tree view:
  * version_family(doc_id) -> the connected set of documents linked by version
    edges (the "tree" a document belongs to), with per-node metadata and the
    parent/child edges, restricted to one source type (PDF or PPTX).
  * savings(doc_ids)       -> how much space the dedup saved for a family:
    the size if every version stored all its pages/images in full, vs. what is
    actually stored once (unique pages + unique images).
"""
from __future__ import annotations

from .store import Store

RELATION_NAMES = {1: "extends", 2: "shortened", 3: "reordered", 4: "modified"}
SOURCE_NAMES = {1: "pdf", 2: "pptx"}


def version_family(store: Store, doc_id: int) -> dict:
    """Return the version family (connected component) containing doc_id.

    {
      "root": <oldest node id>,
      "source_type": "pdf"|"pptx",
      "nodes": [ {id, name, date, pages, source_type, age_rank, ...}, ... ],
      "edges": [ {parent, child, relation, added, removed}, ... ],
    }
    The family is restricted to nodes of the SAME source_type as doc_id, so PDF
    and PPTX trees stay separate.
    """
    base = store.conn.execute(
        "SELECT id, source_type FROM document WHERE id = ?", (doc_id,)
    ).fetchone()
    if base is None:
        return {}
    source_type = base["source_type"]

    # Walk edges (both directions) to collect the connected component.
    seen: set[int] = set()
    frontier = [doc_id]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        rows = store.conn.execute(
            "SELECT child_id AS other FROM document_edge WHERE parent_id = ? "
            "UNION SELECT parent_id AS other FROM document_edge WHERE child_id = ?",
            (cur, cur),
        ).fetchall()
        for r in rows:
            if r["other"] not in seen:
                frontier.append(r["other"])

    # Keep only same-source-type nodes (separate PDF vs PPTX trees).
    node_rows = []
    for nid in seen:
        row = store.conn.execute(
            "SELECT id, source_type, source_name, title, page_count, created_at "
            "FROM document WHERE id = ?", (nid,)
        ).fetchone()
        if row and row["source_type"] == source_type:
            node_rows.append(row)

    node_ids = {r["id"] for r in node_rows}
    # Age rank: oldest = 0 (thickest/trunk), newest = highest (thin twig).
    node_rows.sort(key=lambda r: r["created_at"] or "")
    nodes = []
    for rank, r in enumerate(node_rows):
        nodes.append({
            # ids are 64-bit ints -> send as STRINGS so JS doesn't round them
            # (JS numbers lose precision beyond 2^53, which breaks /api/tree/<id>).
            "id": str(r["id"]),
            "name": r["source_name"] or r["title"] or f"doc {r['id']}",
            "date": r["created_at"],
            "pages": r["page_count"],
            "source_type": SOURCE_NAMES.get(r["source_type"], "?"),
            "age_rank": rank,                 # 0 = oldest
            "is_root": rank == 0,
        })

    edges = []
    for e in store.conn.execute(
        "SELECT parent_id, child_id, relation, added, removed FROM document_edge"
    ).fetchall():
        if e["parent_id"] in node_ids and e["child_id"] in node_ids:
            edges.append({
                "parent": str(e["parent_id"]), "child": str(e["child_id"]),
                "relation": RELATION_NAMES.get(e["relation"], "?"),
                "added": e["added"], "removed": e["removed"],
            })

    root = node_rows[0]["id"] if node_rows else doc_id
    return {
        "root": str(root),
        "source_type": SOURCE_NAMES.get(source_type, "?"),
        "nodes": nodes,
        "edges": edges,
    }


def savings(store: Store, doc_ids: list[int]) -> dict:
    """Space saved across a set of documents (a family) by page + image dedup.

    full   = bytes if every document stored ALL its pages/images in full
             (i.e. duplicates counted once per occurrence).
    stored = bytes actually on disk (each unique page/image once).
    saved  = full - stored.
    """
    if not doc_ids:
        return {"full_bytes": 0, "stored_bytes": 0, "saved_bytes": 0, "saved_percent": 0.0}

    qmarks = ",".join("?" * len(doc_ids))

    # --- pages ---
    # full: sum of page.byte_size over every (document, page) occurrence.
    full_pages = store.conn.execute(
        f"""SELECT COALESCE(SUM(p.byte_size),0)
            FROM document_page dp JOIN page p ON p.hash = dp.page_hash
            WHERE dp.document_id IN ({qmarks})""",
        doc_ids,
    ).fetchone()[0]
    # stored: sum of page.byte_size over DISTINCT pages used by these docs.
    stored_pages = store.conn.execute(
        f"""SELECT COALESCE(SUM(p.byte_size),0) FROM page p
            WHERE p.hash IN (
              SELECT DISTINCT page_hash FROM document_page WHERE document_id IN ({qmarks})
            )""",
        doc_ids,
    ).fetchone()[0]

    # --- images ---
    # full: every (page, image) occurrence for pages in these docs, image.size each.
    full_images = store.conn.execute(
        f"""SELECT COALESCE(SUM(i.size),0)
            FROM document_page dp
            JOIN page_image pi ON pi.page_hash = dp.page_hash
            JOIN image_hash ih ON ih.hash = pi.image_hash
            JOIN image i ON i.id = ih.image_id
            WHERE dp.document_id IN ({qmarks})""",
        doc_ids,
    ).fetchone()[0]
    # stored: each unique image once.
    stored_images = store.conn.execute(
        f"""SELECT COALESCE(SUM(i.size),0) FROM image i
            WHERE i.id IN (
              SELECT DISTINCT ih.image_id
              FROM document_page dp
              JOIN page_image pi ON pi.page_hash = dp.page_hash
              JOIN image_hash ih ON ih.hash = pi.image_hash
              WHERE dp.document_id IN ({qmarks})
            )""",
        doc_ids,
    ).fetchone()[0]

    full = full_pages + full_images
    stored = stored_pages + stored_images
    saved = max(0, full - stored)
    pct = (saved / full * 100.0) if full else 0.0
    return {
        "full_bytes": full, "stored_bytes": stored,
        "saved_bytes": saved, "saved_percent": round(pct, 1),
        "detail": {
            "pages": {"full": full_pages, "stored": stored_pages},
            "images": {"full": full_images, "stored": stored_images},
        },
    }


def family_with_savings(store: Store, doc_id: int) -> dict:
    """Convenience: the family plus its savings, for one API call."""
    fam = version_family(store, doc_id)
    if not fam:
        return {}
    # node ids are stringified for JS safety; savings SQL needs the ints back
    fam["savings"] = savings(store, [int(n["id"]) for n in fam["nodes"]])
    return fam

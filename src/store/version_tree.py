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
            "SELECT child_id AS other FROM document_edge WHERE parent_id = ? AND shared >= 3 "
            "UNION SELECT parent_id AS other FROM document_edge WHERE child_id = ? AND shared >= 3",
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


def _scan_saved_families(store: Store) -> list[dict]:
    """Classify every family that SAVED space. Each entry includes a slides vs
    pictures breakdown and the family size. Used to split version families
    (size>=2) from single-document picture-savers (size==1)."""
    rows = store.conn.execute(
        "SELECT id, source_type, source_name, title, created_at FROM document"
    ).fetchall()
    seen: set[int] = set()
    out = []
    for r in rows:
        if r["id"] in seen:
            continue
        fam = version_family(store, r["id"])
        if not fam:
            continue
        ids = [int(n["id"]) for n in fam["nodes"]]
        seen.update(ids)
        sv = savings(store, ids)
        if sv["saved_bytes"] <= 0:
            continue                       # nothing saved -> not interesting
        detail = sv.get("detail", {})
        slides_saved = max(0, detail.get("pages", {}).get("full", 0) - detail.get("pages", {}).get("stored", 0))
        images_saved = max(0, detail.get("images", {}).get("full", 0) - detail.get("images", {}).get("stored", 0))
        root_node = fam["nodes"][0] if fam["nodes"] else None
        out.append({
            "root": fam["root"],
            "name": root_node["name"] if root_node else (r["source_name"] or f"doc {r['id']}"),
            "source_type": fam["source_type"],
            "size": len(fam["nodes"]),
            "saved_bytes": sv["saved_bytes"],
            "saved_percent": sv["saved_percent"],
            "slides_saved": slides_saved,
            "images_saved": images_saved,
        })
    return out


def all_families(store: Store) -> list[dict]:
    """Multi-version families (size >= 2) that saved via shared slides/pictures.

    Single-document savers (one file that only saved via its own internal
    picture dedup) are NOT here — see single_savers(). For the /versions page.
    """
    out = [f for f in _scan_saved_families(store) if f["size"] >= 2]
    out.sort(key=lambda f: (-f["size"], -f["saved_bytes"]))
    return out


def single_savers(store: Store) -> list[dict]:
    """Single-document savers: one file (no version siblings) that still saved
    space via internal picture dedup. For the dedupe page's table."""
    out = [f for f in _scan_saved_families(store) if f["size"] == 1]
    out.sort(key=lambda f: -f["saved_bytes"])
    return out


def _slide_size(store: Store, page_hash: str) -> int:
    """Total bytes of a slide/page = its text bytes + the bytes of its images."""
    row = store.conn.execute("SELECT byte_size FROM page WHERE hash = ?", (page_hash,)).fetchone()
    text_bytes = row["byte_size"] if row else 0
    img_bytes = store.conn.execute(
        """SELECT COALESCE(SUM(i.size),0)
           FROM page_image pi
           JOIN image_hash ih ON ih.hash = pi.image_hash
           JOIN image i ON i.id = ih.image_id
           WHERE pi.page_hash = ?""",
        (page_hash,),
    ).fetchone()[0]
    return int(text_bytes) + int(img_bytes)


def hypergraph(store: Store, doc_id: int) -> dict:
    """Slide/version membership graph with FAIR-SHARE per-version sizing.

    Nodes:
      versions: {id, name, original_bytes, fairshare_bytes, source_type}
      slides:   {hash, size, used_by}   (used_by = how many versions include it)
    Edges: (version_id -> slide_hash) membership.

    fairshare_bytes for a version =
        Σ over its slides of  slide_size / (#versions in the family using that slide)
    So a slide shared by N versions charges each version slide_size/N. Unshared
    slides (used_by == 1) are charged in full. The sum of all versions'
    fairshare_bytes ≈ the unique bytes actually stored for the family.
    """
    fam = version_family(store, doc_id)
    if not fam:
        return {}
    version_ids = [int(n["id"]) for n in fam["nodes"]]
    names = {int(n["id"]): n["name"] for n in fam["nodes"]}

    # membership: version -> [page_hash], and page -> set(versions)
    membership = {}
    page_versions = {}
    for vid in version_ids:
        hashes = store.document_page_hashes(vid)
        membership[vid] = hashes
        for h in set(hashes):
            page_versions.setdefault(h, set()).add(vid)

    # slide sizes (once per unique page)
    slide_size = {h: _slide_size(store, h) for h in page_versions}

    # original uploaded size per version
    original = {}
    for vid in version_ids:
        row = store.conn.execute("SELECT file_size FROM document WHERE id = ?", (vid,)).fetchone()
        original[vid] = int(row["file_size"]) if row else 0

    # fair-share per version (dedup within a version: count each unique slide once)
    fairshare = {vid: 0.0 for vid in version_ids}
    for h, vers in page_versions.items():
        share = slide_size[h] / len(vers)
        for vid in vers:
            fairshare[vid] += share

    versions = [{
        "id": str(vid), "name": names[vid], "source_type": fam["source_type"],
        "original_bytes": original[vid],
        "fairshare_bytes": round(fairshare[vid]),
    } for vid in version_ids]

    family_set = set(version_ids)
    slides = []
    for h, vers in page_versions.items():
        # how many documents OUTSIDE this family also contain this slide/page
        rows = store.conn.execute(
            "SELECT DISTINCT document_id FROM document_page WHERE page_hash = ?", (h,)
        ).fetchall()
        external = sum(1 for r in rows if r["document_id"] not in family_set)
        slides.append({
            "hash": h, "size": slide_size[h], "used_by": len(vers),
            "external_uses": external,   # >0 -> this slide bridges to another cluster
        })

    edges = [{"version": str(vid), "slide": h}
             for vid, hashes in membership.items() for h in set(hashes)]

    return {
        "source_type": fam["source_type"],
        "selected": str(doc_id),
        "versions": versions,
        "slides": slides,
        "edges": edges,
    }

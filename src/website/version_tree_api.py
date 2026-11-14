"""
Backend for the version-tree page (tree.html): show the version family of a
document, how much space the dedup saved, and let the user download any node as
a regenerated PDF/PPTX.

URLs:
  GET  /api/tree/documents           list stored documents (id, name, type, pages, date)
  GET  /api/tree/<doc_id>            the version family + per-node metadata + savings
  GET  /api/tree/<doc_id>/download   regenerate that node as a PDF/PPTX download

PDF and PPTX families are separate (version_family restricts to one source_type).
Downloads are content-faithful regenerations (same text + the single stored copy
of each picture), not byte-identical originals; the store recognizes them by
content_hash if re-uploaded.
"""
from __future__ import annotations

import io
from pathlib import Path

from flask import Blueprint, jsonify, send_file

from store import Store
from store.version_tree import family_with_savings, all_families
from store.regenerate import regenerate

PROJECT_DIR = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_DIR / "data" / "database"
OBJECTS_DIR = PROJECT_DIR / "data" / "objects"

bp = Blueprint("version_tree", __name__)


def _store() -> Store:
    return Store(DB_PATH, objects_dir=OBJECTS_DIR)


@bp.route("/api/tree/families")
def list_families():
    """Every version family in the store (for the /versions page's left table)."""
    s = _store()
    try:
        fams = all_families(s)
    finally:
        s.close()
    # stringify root ids for JS 64-bit safety
    for f in fams:
        f["root"] = str(f["root"])
    return jsonify(families=fams)


@bp.route("/api/tree/documents")
def list_documents():
    """All stored documents, newest first, so the UI can pick one to show its tree."""
    s = _store()
    try:
        rows = s.conn.execute(
            "SELECT id, source_name, source_type, page_count, created_at "
            "FROM document ORDER BY created_at DESC"
        ).fetchall()
    finally:
        s.close()
    names = {1: "pdf", 2: "pptx"}
    return jsonify(documents=[
        {"id": str(r["id"]), "name": r["source_name"] or f"doc {r['id']}",
         "type": names.get(r["source_type"], "?"), "pages": r["page_count"],
         "date": r["created_at"]}
        for r in rows
    ])


@bp.route("/api/tree/<doc_id>")
def tree(doc_id):
    """The version family (tree) of a document plus its space savings."""
    try:
        doc_id = int(doc_id)
    except ValueError:
        return jsonify(detail="Invalid document id"), 400
    s = _store()
    try:
        fam = family_with_savings(s, doc_id)
    finally:
        s.close()
    if not fam:
        return jsonify(detail=f"No document with id {doc_id}"), 404
    fam["selected"] = str(doc_id)     # the node the UI should highlight (string: 64-bit safe)
    return jsonify(fam)


@bp.route("/api/tree/<doc_id>/download")
def download_node(doc_id):
    """Download this node as the EXACT original file (pixel/byte-identical).

    Uses the stored restore skeleton (image streams put back exactly). Falls
    back to the old text regeneration only if no skeleton exists for this doc.
    """
    try:
        doc_id = int(doc_id)
    except ValueError:
        return jsonify(detail="Invalid document id"), 400
    s = _store()
    try:
        row = s.conn.execute(
            "SELECT source_type, source_name FROM document WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            return jsonify(detail=f"No document with id {doc_id}"), 404
        source_type = {1: "pdf", 2: "pptx"}.get(row["source_type"], "?")
        name = row["source_name"]

        # Preferred: exact restore from skeleton.
        restored = None
        try:
            restored = s.restore_original(doc_id)
        except Exception:
            restored = None

        if restored is not None:
            data, ext = restored
            mimetype = ("application/pdf" if ext == "pdf"
                        else "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        else:
            # Fallback: old content regeneration (no skeleton available).
            pages = s.reconstruct(doc_id)
            data, mimetype, ext = regenerate(source_type, pages)
    except ValueError as exc:
        return jsonify(detail=str(exc)), 400
    finally:
        s.close()

    stem = Path(name or f"doc_{doc_id}").stem
    return send_file(io.BytesIO(data), mimetype=mimetype, as_attachment=True,
                     download_name=f"{stem}.{ext}")

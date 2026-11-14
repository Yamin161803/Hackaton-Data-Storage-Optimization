#!/usr/bin/env python3
"""Corpus document store: content-addressed documents with page-level dedup.

This is the Tier-1 storage/index layer for PDFs and slide decks. It reuses the
fingerprint idea from src/videohash.py (size -> quick_hash -> full_hash) as a
fast exact-duplicate gate, dedups pages by content hash, and records each
document as an ordered manifest of page hashes.

Key behaviours (see tests/test_store.py):
  * Exact duplicate file  -> discarded (ingest returns status="duplicate").
  * Shared start/end pages -> deduped individually; never a false merge.
  * Shorten a deck + re-add -> NOT deleted, near-zero extra storage (only a new
    tiny manifest), recorded as a derived (subset) version candidate.

Identity is content, never filename. The filename is stored as metadata only.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

SOURCE_PDF = 1
SOURCE_PPTX = 2

REL_EXTENDS = 1    # child is a superset of parent (added pages)
REL_SHORTENED = 2  # child is a subset of parent (removed pages)
REL_REORDERED = 3  # same page set, different order
REL_MODIFIED = 4   # overlapping but neither subset nor superset

# image format codes (match the existing `image` table: 1=avif 2=jxl 3=webp 4=jpeg 5=png)
_FMT_BY_EXT = {".avif": 1, ".jxl": 2, ".webp": 3, ".jpg": 4, ".jpeg": 4, ".png": 5}


def _image_ext(data: bytes) -> str:
    """Guess an image file extension from magic bytes (png/jpeg/webp/gif)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".bin"


# ---------- hashing (mirrors videohash.py conventions) ----------

def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for i, p in enumerate(parts):
        if i:
            h.update(b"\x00")
        h.update((p or "").encode("utf-8"))
    return h.hexdigest()


def normalize_text(text: str) -> str:
    """Normalize page/slide text the same way src/dedupe/slide_dedupe.py does.

    Collapses whitespace, drops empty lines and page-number/footer lines, so a
    slide re-exported or moved to a new position still hashes the same. We reuse
    the dedupe implementation when available so both code paths agree exactly;
    the fallback keeps the store usable standalone (e.g. without pypdf/pptx).
    """
    try:
        from dedupe.slide_dedupe import normalize_text as _dedupe_normalize
        return _dedupe_normalize(text, drop_page_numbers=True)
    except Exception:
        return "\n".join(" ".join(line.split()) for line in (text or "").splitlines()
                         if line.split())


def hash_text(text: str) -> str:
    """Hash of a page's text. For PPTX this is the SPEAKER NOTES; for PDF the page text.

    Uses the shared normalization so the store and slide_dedupe agree on what
    'the same text' means.
    """
    return _sha(normalize_text(text))


def hash_images(image_hashes: list[str] | None) -> str:
    """Hash of a page's picture set (order-independent, duplicates ignored).

    Matches slide_dedupe.slide_hash's sorted(set(...)) handling of image hashes.
    """
    return _sha(*sorted(set(image_hashes or [])))


def combined_hash(text_hash: str, image_hash: str) -> str:
    """Overall page identity from the two sub-hashes."""
    return _sha(text_hash, image_hash)


def hash_page(text: str, image_hashes: list[str] | None = None) -> str:
    """Overall page identity: combines normalized text + referenced image hashes.

    Kept for backward compatibility / convenience; equals
    combined_hash(hash_text(text), hash_images(image_hashes)).
    """
    return combined_hash(hash_text(text), hash_images(image_hashes))


def _id_from_hash(hex_digest: str) -> int:
    """First 8 bytes of the hash as a signed int64 (SQLite INTEGER PRIMARY KEY).

    Same convention as videohash._till_id so ids are consistent across the app.
    """
    return int.from_bytes(bytes.fromhex(hex_digest)[:8], "big", signed=True)


# ---------- data carried in from the extractors (pdf_handler / pptx handler) ----------

@dataclass
class Page:
    """One page/slide produced by an extractor.

    * text         -- PPTX: speaker notes; PDF: page text.
    * image_hashes -- content hashes of the picture(s) on the page/slide.

    Identity:
      PDF : text and image move together (combined identity).
      PPTX: text_hash (notes) and image_hash (slide picture) are tracked
            separately, so "same picture, rewritten notes" is a visual duplicate.
    The overall `hash` is combined_hash(text_hash, image_hash) in both cases;
    the difference is in how dedup/version logic *uses* the sub-hashes (see Store).
    """
    text: str
    image_hashes: list[str] = field(default_factory=list)
    image_id: int | None = None

    @property
    def text_hash(self) -> str:
        return hash_text(self.text)

    @property
    def image_hash(self) -> str:
        return hash_images(self.image_hashes)

    @property
    def hash(self) -> str:
        return combined_hash(self.text_hash, self.image_hash)


@dataclass
class DocumentInput:
    """A document to ingest: its file fingerprint + its ordered pages."""
    full_hash: str
    file_size: int
    source_type: int
    pages: list[Page]
    quick_hash: str | None = None
    source_name: str | None = None
    title: str | None = None


@dataclass
class IngestResult:
    status: str                 # "stored" | "duplicate" (byte) | "content_duplicate"
    document_id: int | None
    new_pages: int = 0          # pages whose content was not already stored
    shared_pages: int = 0       # pages already present (dedup hits)
    version_candidates: list[int] = field(default_factory=list)  # doc ids sharing >=1 page
    # PPTX only: slides whose PICTURE matches an existing slide but whose NOTES
    # differ -> "same slide, rewritten notes". Each entry:
    #   {"new_page_hash", "matched_page_hash", "image_hash"}
    visual_duplicates: list[dict] = field(default_factory=list)


# ---------- store ----------

class Store:
    def __init__(self, db_path: str | Path, objects_dir: str | Path | None = None):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # Image blobs live on disk, content-addressed and sharded by hash prefix.
        # Default: an "objects" dir next to the database file.
        self.objects_dir = Path(objects_dir) if objects_dir else \
            Path(db_path).resolve().parent / "objects"
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def reset(self) -> dict:
        """Wipe the corpus store: all documents/pages/images and the object blobs.

        DESTRUCTIVE. Clears the corpus tables and deletes the content-addressed
        image blobs under objects_dir. Returns counts of what was removed.
        The schema is recreated so the store is immediately usable again.
        """
        counts = {}
        for tbl in ("document", "page", "document_page", "document_edge",
                    "page_image", "image", "image_hash"):
            try:
                counts[tbl] = self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:
                counts[tbl] = 0
        # Drop corpus tables (leave any non-corpus tables in the shared DB alone).
        for tbl in ("document_edge", "document_page", "page_image", "page_fts",
                    "page", "image_hash", "image", "document"):
            self.conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        self.conn.commit()

        blobs_removed = 0
        if self.objects_dir.exists():
            for p in self.objects_dir.rglob("*"):
                if p.is_file():
                    p.unlink()
                    blobs_removed += 1
            # remove now-empty shard dirs
            for p in sorted(self.objects_dir.rglob("*"), reverse=True):
                if p.is_dir():
                    try:
                        p.rmdir()
                    except OSError:
                        pass
        counts["blobs"] = blobs_removed

        self._ensure_schema()   # recreate empty tables
        return counts

    # -- queries --

    def find_document_by_full_hash(self, full_hash: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM document WHERE full_hash = ?", (full_hash,)
        ).fetchone()
        return row["id"] if row else None

    def page_exists(self, page_hash: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM page WHERE hash = ?", (page_hash,)
        ).fetchone() is not None

    def documents_sharing_pages(self, page_hashes: list[str]) -> dict[int, int]:
        """{document_id: number_of_shared_pages} for docs sharing >=1 of these pages.

        Uses the inverted index idx_document_page_hash — this is what keeps
        version detection fast at scale (no full scan).
        """
        counts: dict[int, int] = {}
        if not page_hashes:
            return counts
        qmarks = ",".join("?" * len(page_hashes))
        rows = self.conn.execute(
            f"SELECT document_id, COUNT(*) AS n FROM document_page "
            f"WHERE page_hash IN ({qmarks}) GROUP BY document_id",
            page_hashes,
        ).fetchall()
        for r in rows:
            counts[r["document_id"]] = r["n"]
        return counts

    def document_page_hashes(self, document_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT page_hash FROM document_page WHERE document_id = ? ORDER BY position",
            (document_id,),
        ).fetchall()
        return [r["page_hash"] for r in rows]

    def visual_duplicates(self, image_hash: str, exclude_page_hash: str | None = None) -> list[dict]:
        """Existing pages that share this picture (same image_hash).

        Used for PPTX: a slide with the SAME picture but DIFFERENT speaker notes
        is a visual duplicate / reworded slide. Pages with an empty picture are
        never treated as visual duplicates of each other (nothing to match on).
        """
        if not image_hash or image_hash == hash_images([]):
            return []
        rows = self.conn.execute(
            "SELECT hash, text_hash, image_hash FROM page WHERE image_hash = ?",
            (image_hash,),
        ).fetchall()
        out = []
        for r in rows:
            if exclude_page_hash and r["hash"] == exclude_page_hash:
                continue
            out.append({"page_hash": r["hash"], "text_hash": r["text_hash"],
                        "image_hash": r["image_hash"]})
        return out

    # -- ingest --

    def _content_hash(self, page_hashes: list[str]) -> str:
        """Regeneration-stable document identity: hash of the ordered page hashes.

        Unlike the full-file byte hash, this is identical for a file and any
        content-faithful regeneration of it (same pages, same order), so a
        downloaded-then-reuploaded node is recognized as the same content.
        """
        return _sha(*page_hashes)

    def find_document_by_content_hash(self, content_hash: str) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM document WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return row["id"] if row else None

    def ingest(self, doc: DocumentInput, record_edges: bool = True) -> IngestResult:
        """Ingest a document. Byte-identical and content-identical files are discarded.

        Two dedup gates:
          1. full_hash    -- exact byte duplicate (fast, certain).
          2. content_hash -- same ordered pages, possibly different bytes
                             (recognizes a regenerated-then-reuploaded file).
        Returns an IngestResult (stored / duplicate / content_duplicate).
        """
        # 1. Exact-duplicate gate (full-file hash). Cheapest/most certain.
        existing = self.find_document_by_full_hash(doc.full_hash)
        if existing is not None:
            return IngestResult(status="duplicate", document_id=existing)

        page_hashes = [p.hash for p in doc.pages]

        # 2. Content-duplicate gate: same pages in the same order, different bytes.
        content_hash = self._content_hash(page_hashes)
        content_existing = self.find_document_by_content_hash(content_hash)
        if content_existing is not None:
            return IngestResult(status="content_duplicate", document_id=content_existing)

        # 3. Find version candidates BEFORE inserting (docs sharing any page).
        shared_counts = self.documents_sharing_pages(page_hashes)
        candidates = sorted(shared_counts, key=lambda d: -shared_counts[d])

        # 4. Insert document row.
        doc_id = _id_from_hash(doc.full_hash)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO document (id, full_hash, content_hash, quick_hash, file_size, "
            "source_type, source_name, title, page_count, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc_id, doc.full_hash, content_hash, doc.quick_hash, doc.file_size,
             doc.source_type, doc.source_name, doc.title, len(doc.pages), now),
        )

        # 4. Dedup + store pages, write the ordered manifest.
        new_pages = shared_pages = 0
        visual_dups: list[dict] = []
        for position, page in enumerate(doc.pages):
            ph = page.hash
            # PPTX only: before this page becomes a new row, is its PICTURE already
            # used by an existing slide with DIFFERENT notes? -> visual duplicate.
            if doc.source_type == SOURCE_PPTX and not self.page_exists(ph):
                for match in self.visual_duplicates(page.image_hash, exclude_page_hash=ph):
                    if match["text_hash"] != page.text_hash:
                        visual_dups.append({
                            "new_page_hash": ph,
                            "matched_page_hash": match["page_hash"],
                            "image_hash": page.image_hash,
                        })
            if self.page_exists(ph):
                shared_pages += 1
            else:
                self.conn.execute(
                    "INSERT INTO page (hash, text_hash, image_hash, text, image_id, byte_size) "
                    "VALUES (?,?,?,?,?,?)",
                    (ph, page.text_hash, page.image_hash, page.text, page.image_id,
                     len((page.text or "").encode("utf-8"))),
                )
                self.conn.execute(
                    "INSERT INTO page_fts (text, hash) VALUES (?, ?)", (page.text or "", ph)
                )
                new_pages += 1
            self.conn.execute(
                "INSERT INTO document_page (document_id, position, page_hash) VALUES (?,?,?)",
                (doc_id, position, ph),
            )

        # 5. Optionally record version lineage edges to candidates.
        if record_edges:
            self._record_edges(doc_id, set(page_hashes), candidates)

        self.conn.commit()
        return IngestResult(
            status="stored", document_id=doc_id,
            new_pages=new_pages, shared_pages=shared_pages,
            version_candidates=candidates,
            visual_duplicates=visual_dups,
        )

    def _record_edges(self, child_id: int, child_pages: set[str], candidates: list[int]) -> None:
        for parent_id in candidates:
            parent_pages = set(self.document_page_hashes(parent_id))
            if not (parent_pages & child_pages):
                continue
            added = len(child_pages - parent_pages)
            removed = len(parent_pages - child_pages)
            if added and not removed:
                relation = REL_EXTENDS
            elif removed and not added:
                relation = REL_SHORTENED
            elif not added and not removed:
                relation = REL_REORDERED   # same set (order differs or identical sequence)
            else:
                relation = REL_MODIFIED
            self.conn.execute(
                "INSERT OR REPLACE INTO document_edge "
                "(parent_id, child_id, relation, added, removed) VALUES (?,?,?,?,?)",
                (parent_id, child_id, relation, added, removed),
            )

    # -- shared image object store --

    def put_image(self, data: bytes, width: int | None = None,
                  height: int | None = None) -> str:
        """Store image bytes once, content-addressed. Returns the full hex hash.

        If an image with the same bytes is already stored, nothing is written
        (dedup): 100 documents sharing one picture keep a single blob on disk.
        """
        hex_hash = hashlib.sha256(data).hexdigest()
        # already stored?
        row = self.conn.execute(
            "SELECT 1 FROM image_hash WHERE hash = ?", (hex_hash,)
        ).fetchone()
        if row:
            return hex_hash

        image_id = _id_from_hash(hex_hash)
        ext = _image_ext(data)
        fmt = _FMT_BY_EXT.get(ext, 0)
        # sharded path: objects/ab/cd/<hash><ext> keeps any dir small at scale.
        rel = Path(hex_hash[:2]) / hex_hash[2:4] / f"{hex_hash}{ext}"
        dest = self.objects_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        # image.id is a PRIMARY KEY; guard against the rare 8-byte-prefix clash.
        self.conn.execute(
            "INSERT OR IGNORE INTO image (id, path, fmt, size, width, height) "
            "VALUES (?,?,?,?,?,?)",
            (image_id, str(rel), fmt, len(data), width, height),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO image_hash (hash, image_id) VALUES (?, ?)",
            (hex_hash, image_id),
        )
        self.conn.commit()
        return hex_hash

    def link_page_images(self, page_hash: str, image_hashes: list[str]) -> None:
        """Record which images (by full hex hash) a page references, in order."""
        for position, ih in enumerate(image_hashes):
            self.conn.execute(
                "INSERT OR REPLACE INTO page_image (page_hash, image_hash, position) "
                "VALUES (?,?,?)",
                (page_hash, ih, position),
            )
        self.conn.commit()

    def image_path(self, image_hash: str) -> Path | None:
        """Absolute path to a stored image blob, by its full hex hash."""
        row = self.conn.execute(
            "SELECT i.path FROM image_hash h JOIN image i ON i.id = h.image_id "
            "WHERE h.hash = ?", (image_hash,)
        ).fetchone()
        return (self.objects_dir / row["path"]) if row else None

    def image_refcount(self, image_hash: str) -> int:
        """How many pages reference this image (0 = safe to delete)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM page_image WHERE image_hash = ?", (image_hash,)
        ).fetchone()[0]

    def reconstruct(self, document_id: int) -> list[dict]:
        """Everything needed to rebuild a document, in order.

        Returns a list of page dicts: {"position", "page_hash", "text",
        "images": [absolute image paths]} — the text plus the single stored copy
        of each referenced picture, so the original PDF/deck can be remade.
        """
        rows = self.conn.execute(
            "SELECT dp.position, p.hash, p.text FROM document_page dp "
            "JOIN page p ON p.hash = dp.page_hash "
            "WHERE dp.document_id = ? ORDER BY dp.position",
            (document_id,),
        ).fetchall()
        out = []
        for r in rows:
            imgs = self.conn.execute(
                "SELECT image_hash FROM page_image WHERE page_hash = ? ORDER BY position",
                (r["hash"],),
            ).fetchall()
            paths = [str(self.image_path(i["image_hash"])) for i in imgs]
            out.append({"position": r["position"], "page_hash": r["hash"],
                        "text": r["text"], "images": paths})
        return out

    # -- pixel-identical skeleton store/restore --

    def image_store(self):
        """A shared ImageStore rooted at this store's objects dir (skeleton images)."""
        from .image_store import ImageStore
        return ImageStore(self.objects_dir / "skeleton_images")

    def save_skeleton(self, document_id: int, kind: str, skeleton_json: str,
                      skeleton_blob: bytes | None = None) -> None:
        """Persist the restore skeleton for a document (pdf/pptx)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO document_skeleton "
            "(document_id, kind, skeleton_json, skeleton_blob) VALUES (?,?,?,?)",
            (document_id, kind, skeleton_json, skeleton_blob),
        )
        self.conn.commit()

    def get_skeleton(self, document_id: int) -> dict | None:
        """Return {kind, skeleton_json, skeleton_blob} or None if no skeleton stored."""
        row = self.conn.execute(
            "SELECT kind, skeleton_json, skeleton_blob FROM document_skeleton "
            "WHERE document_id = ?", (document_id,),
        ).fetchone()
        if row is None:
            return None
        blob = row["skeleton_blob"]
        return {"kind": row["kind"], "skeleton_json": row["skeleton_json"],
                "skeleton_blob": bytes(blob) if blob is not None else None}

    def restore_original(self, document_id: int) -> tuple[bytes, str] | None:
        """Rebuild the EXACT original file from its skeleton. Returns (bytes, ext).

        Pixel-identical for PDF, byte-identical for PPTX. None if no skeleton.
        """
        import json
        skel = self.get_skeleton(document_id)
        if skel is None:
            return None
        images = self.image_store()
        if skel["kind"] == "pdf":
            from .pdf_skeleton import restore_pdf
            data = restore_pdf(skel["skeleton_blob"], json.loads(skel["skeleton_json"]), images)
            return data, "pdf"
        if skel["kind"] == "pptx":
            from .pptx_skeleton import restore_pptx
            data = restore_pptx(json.loads(skel["skeleton_json"]), images)
            return data, "pptx"
        return None

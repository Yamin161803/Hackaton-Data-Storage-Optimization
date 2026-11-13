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


# ---------- hashing (mirrors videohash.py conventions) ----------

def hash_page(text: str, image_hashes: list[str] | None = None) -> str:
    """Content hash of a page: normalized text + any referenced image hashes.

    Hashing normalized *content* (not raw file bytes) makes a page robust to
    re-exports / metadata changes: the same slide re-saved still matches.
    """
    h = hashlib.sha256()
    h.update((text or "").strip().encode("utf-8"))
    for ih in sorted(image_hashes or []):
        h.update(b"\x00")
        h.update(ih.encode("utf-8"))
    return h.hexdigest()


def _id_from_hash(hex_digest: str) -> int:
    """First 8 bytes of the hash as a signed int64 (SQLite INTEGER PRIMARY KEY).

    Same convention as videohash._till_id so ids are consistent across the app.
    """
    return int.from_bytes(bytes.fromhex(hex_digest)[:8], "big", signed=True)


# ---------- data carried in from the extractors (pdf_handler / pptx handler) ----------

@dataclass
class Page:
    """One page/slide produced by an extractor."""
    text: str
    image_hashes: list[str] = field(default_factory=list)
    image_id: int | None = None

    @property
    def hash(self) -> str:
        return hash_page(self.text, self.image_hashes)


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
    status: str                 # "stored" | "duplicate"
    document_id: int | None
    new_pages: int = 0          # pages whose content was not already stored
    shared_pages: int = 0       # pages already present (dedup hits)
    version_candidates: list[int] = field(default_factory=list)  # doc ids sharing >=1 page


# ---------- store ----------

class Store:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

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

    # -- ingest --

    def ingest(self, doc: DocumentInput, record_edges: bool = True) -> IngestResult:
        """Ingest a document. Exact duplicates are discarded.

        Returns an IngestResult describing what happened (stored vs duplicate,
        how many pages were new vs shared, and which existing documents share
        pages — the candidates for a version relationship).
        """
        # 1. Exact-duplicate gate (full-file hash). Cheapest/most certain.
        existing = self.find_document_by_full_hash(doc.full_hash)
        if existing is not None:
            return IngestResult(status="duplicate", document_id=existing)

        page_hashes = [p.hash for p in doc.pages]

        # 2. Find version candidates BEFORE inserting (docs sharing any page).
        shared_counts = self.documents_sharing_pages(page_hashes)
        candidates = sorted(shared_counts, key=lambda d: -shared_counts[d])

        # 3. Insert document row.
        doc_id = _id_from_hash(doc.full_hash)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO document (id, full_hash, quick_hash, file_size, source_type, "
            "source_name, title, page_count, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (doc_id, doc.full_hash, doc.quick_hash, doc.file_size, doc.source_type,
             doc.source_name, doc.title, len(doc.pages), now),
        )

        # 4. Dedup + store pages, write the ordered manifest.
        new_pages = shared_pages = 0
        for position, page in enumerate(doc.pages):
            ph = page.hash
            if self.page_exists(ph):
                shared_pages += 1
            else:
                self.conn.execute(
                    "INSERT INTO page (hash, text, image_id, byte_size) VALUES (?,?,?,?)",
                    (ph, page.text, page.image_id, len((page.text or "").encode("utf-8"))),
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

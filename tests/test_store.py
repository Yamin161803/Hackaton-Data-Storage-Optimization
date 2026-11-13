"""Tests for the corpus document store (src/store).

Proves the design decisions from the versioning discussion:
  * exact-duplicate files are discarded;
  * shared start/end slides dedup individually and do NOT cause a false merge
    even when everything in the middle is unique (no overlap-threshold trap);
  * shortening a deck and re-adding it does NOT delete anything, costs near-zero
    extra storage (only a new manifest), and is recorded as a subset version;
  * pages are content-addressed, so identity is content, not filename.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from store import Store, DocumentInput, Page, SOURCE_PPTX, SOURCE_PDF  # noqa: E402
from store.store import REL_SHORTENED, REL_EXTENDS  # noqa: E402


def slide(text: str) -> Page:
    return Page(text=text)


def slide_img(notes: str, image_hashes: list[str]) -> Page:
    """A PPTX slide: picture(s) + speaker notes (the 'text')."""
    return Page(text=notes, image_hashes=image_hashes)


def make_doc(pages, name, full_hash=None):
    """A DocumentInput from a list of slide texts. full_hash defaults to a hash
    of the concatenated content (so same content -> same file hash by default)."""
    page_objs = [slide(t) for t in pages]
    if full_hash is None:
        full_hash = hashlib.sha256("|".join(pages).encode()).hexdigest()
    return DocumentInput(
        full_hash=full_hash,
        file_size=sum(len(t) for t in pages),
        source_type=SOURCE_PPTX,
        pages=page_objs,
        source_name=name,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def pages_in_store(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM page").fetchone()[0]


def docs_in_store(store) -> int:
    return store.conn.execute("SELECT COUNT(*) FROM document").fetchone()[0]


# ---------- exact duplicate discard ----------

def test_exact_duplicate_is_discarded(store):
    doc = make_doc(["intro", "body", "outro"], "deck.pptx")
    first = store.ingest(doc)
    assert first.status == "stored"

    # Same file content again (same full_hash) -> discarded.
    again = make_doc(["intro", "body", "outro"], "deck-copy.pptx")
    second = store.ingest(again)
    assert second.status == "duplicate"
    assert second.document_id == first.document_id
    assert docs_in_store(store) == 1
    assert pages_in_store(store) == 3


# ---------- shared start/end slides, unique middle (no false merge) ----------

def test_shared_start_and_end_slides_dedup_without_false_merge(store):
    # Two decks share ONLY the title and closing slide; middles are all unique.
    a = make_doc(["TITLE", "a1", "a2", "a3", "CLOSING"], "deckA.pptx")
    b = make_doc(["TITLE", "b1", "b2", "b3", "CLOSING"], "deckB.pptx")

    ra = store.ingest(a)
    rb = store.ingest(b)

    assert ra.status == "stored" and rb.status == "stored"
    assert docs_in_store(store) == 2            # both kept; NOT merged

    # Shared pages (TITLE, CLOSING) stored once; unique middles stored separately.
    # Unique pages: TITLE, CLOSING, a1,a2,a3, b1,b2,b3 = 8 distinct pages.
    assert pages_in_store(store) == 8
    assert rb.shared_pages == 2                 # TITLE + CLOSING already present
    assert rb.new_pages == 3                    # b1,b2,b3

    # B shares 2 pages with A, so A is a version candidate, but the relationship
    # is "modified" (added AND removed), never a merge/collapse of the documents.
    assert ra.document_id in rb.version_candidates


# ---------- shorten-and-re-add: not deleted, near-zero storage, subset version ----------

def test_shorten_and_readd_keeps_doc_and_adds_almost_no_storage(store):
    full = make_doc(["s1", "s2", "s3", "s4", "s5"], "original.pptx")
    r_full = store.ingest(full)
    assert r_full.status == "stored"
    pages_after_full = pages_in_store(store)
    assert pages_after_full == 5

    # Shorten: remove 2 slides, RENAME the file, re-ingest.
    shortened = make_doc(["s1", "s2", "s3"], "renamed-shorter.pptx")
    r_short = store.ingest(shortened)

    # 1. NOT discarded (different file content => different full_hash).
    assert r_short.status == "stored"
    assert docs_in_store(store) == 2

    # 2. Near-zero extra storage: all 3 pages already existed => no new pages.
    assert r_short.new_pages == 0
    assert r_short.shared_pages == 3
    assert pages_in_store(store) == pages_after_full   # storage unchanged

    # 3. Recorded as a derived (shortened/subset) version of the original.
    assert r_full.document_id in r_short.version_candidates
    edge = store.conn.execute(
        "SELECT relation, added, removed FROM document_edge "
        "WHERE parent_id = ? AND child_id = ?",
        (r_full.document_id, r_short.document_id),
    ).fetchone()
    assert edge is not None
    assert edge["relation"] == REL_SHORTENED
    assert edge["added"] == 0
    assert edge["removed"] == 2


# ---------- extend: superset recorded as 'extends' ----------

def test_extending_a_deck_is_recorded_as_extends(store):
    base = make_doc(["s1", "s2", "s3"], "base.pptx")
    r_base = store.ingest(base)
    extended = make_doc(["s1", "s2", "s3", "s4", "s5"], "extended.pptx")
    r_ext = store.ingest(extended)

    assert r_ext.new_pages == 2 and r_ext.shared_pages == 3
    edge = store.conn.execute(
        "SELECT relation, added, removed FROM document_edge "
        "WHERE parent_id = ? AND child_id = ?",
        (r_base.document_id, r_ext.document_id),
    ).fetchone()
    assert edge["relation"] == REL_EXTENDS
    assert edge["added"] == 2 and edge["removed"] == 0


# ---------- identity is content, not filename ----------

def test_identity_is_content_not_filename(store):
    # Same content, totally different names -> treated as the same file (dup).
    a = make_doc(["x", "y"], "completely-different-name-1.pptx")
    b = make_doc(["x", "y"], "completely-different-name-2.pptx")
    assert store.ingest(a).status == "stored"
    assert store.ingest(b).status == "duplicate"


# ---------- full-text search works through the index ----------

def test_fts_search_finds_pages(store):
    store.ingest(make_doc(["quarterly budget forecast", "team roadmap"], "q3.pptx"))
    rows = store.conn.execute(
        "SELECT hash FROM page_fts WHERE page_fts MATCH 'budget'"
    ).fetchall()
    assert len(rows) == 1


# ---------- PPTX: picture + speaker-notes identity ----------

def make_pptx(slides, name, full_hash=None):
    """slides = list of (notes, [image_hashes]) tuples."""
    page_objs = [slide_img(notes, imgs) for notes, imgs in slides]
    if full_hash is None:
        key = "|".join(notes + ":" + ",".join(imgs) for notes, imgs in slides)
        full_hash = hashlib.sha256(key.encode()).hexdigest()
    return DocumentInput(
        full_hash=full_hash, file_size=1, source_type=SOURCE_PPTX,
        pages=page_objs, source_name=name,
    )


def test_pptx_same_picture_rewritten_notes_is_visual_duplicate(store):
    # Deck A: one slide, picture IMG1 + notes "v1".
    a = make_pptx([("original notes", ["IMG1"])], "a.pptx")
    store.ingest(a)

    # Deck B: SAME picture IMG1, but the notes were rewritten.
    b = make_pptx([("completely rewritten notes", ["IMG1"])], "b.pptx")
    rb = store.ingest(b)

    # It's a new page (notes differ -> different overall hash) but flagged as a
    # VISUAL DUPLICATE: same slide picture, reworded notes.
    assert rb.status == "stored"
    assert len(rb.visual_duplicates) == 1
    assert rb.visual_duplicates[0]["image_hash"] == __import__("store").hash_images(["IMG1"])


def test_pptx_same_picture_same_notes_is_exact_page_dup(store):
    a = make_pptx([("notes", ["IMG1"])], "a.pptx")
    store.ingest(a)
    # Different FILE (so not a file-level dup) but the single slide is identical.
    b = make_pptx([("notes", ["IMG1"])], "b.pptx", full_hash="b" * 64)
    rb = store.ingest(b)
    assert rb.status == "stored"
    assert rb.shared_pages == 1 and rb.new_pages == 0   # exact page dedup
    assert rb.visual_duplicates == []                   # not a *reworded* dup


def test_pptx_empty_notes_slides_not_visual_dups_of_each_other(store):
    # Two slides with DIFFERENT pictures and NO notes must not collide.
    a = make_pptx([("", ["IMG1"])], "a.pptx")
    b = make_pptx([("", ["IMG2"])], "b.pptx")
    store.ingest(a)
    rb = store.ingest(b)
    assert rb.new_pages == 1
    assert rb.visual_duplicates == []                   # different pictures


def test_pptx_different_picture_same_notes_is_not_visual_dup(store):
    a = make_pptx([("shared notes", ["IMG1"])], "a.pptx")
    store.ingest(a)
    b = make_pptx([("shared notes", ["IMG2"])], "b.pptx")
    rb = store.ingest(b)
    # Visual-dup is keyed on the PICTURE; different picture -> not a visual dup.
    assert rb.visual_duplicates == []
    assert rb.new_pages == 1


def test_pdf_combined_identity_text_and_image_move_together(store):
    # PDF: same text, different image -> different page (combined identity).
    a = DocumentInput(full_hash="a" * 64, file_size=1, source_type=SOURCE_PDF,
                      pages=[Page(text="report text", image_hashes=["IMGA"])], source_name="a.pdf")
    b = DocumentInput(full_hash="b" * 64, file_size=1, source_type=SOURCE_PDF,
                      pages=[Page(text="report text", image_hashes=["IMGB"])], source_name="b.pdf")
    store.ingest(a)
    rb = store.ingest(b)
    # PDF does not do visual-dup flagging; the two pages are simply distinct.
    assert rb.new_pages == 1
    assert rb.visual_duplicates == []

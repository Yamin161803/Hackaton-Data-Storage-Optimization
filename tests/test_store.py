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


def test_regenerated_file_recognized_by_content_hash(store):
    # A file regenerated from the store has the SAME pages but DIFFERENT bytes
    # (different full_hash). The content_hash gate must recognize it.
    original = make_doc(["intro", "body", "outro"], "deck.pptx", full_hash="a" * 64)
    assert store.ingest(original).status == "stored"

    # Same pages, same order, but a *different* file hash (as a regeneration would be).
    regenerated = make_doc(["intro", "body", "outro"], "deck-regenerated.pptx",
                           full_hash="f" * 64)
    r = store.ingest(regenerated)
    assert r.status == "content_duplicate"      # recognized despite different bytes
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


def test_pptx_same_picture_same_notes_is_content_duplicate(store):
    a = make_pptx([("notes", ["IMG1"])], "a.pptx")
    store.ingest(a)
    # Different FILE bytes, but the single slide is identical (same picture + notes)
    # -> same page set -> recognized as a content duplicate by the content_hash gate.
    b = make_pptx([("notes", ["IMG1"])], "b.pptx", full_hash="b" * 64)
    rb = store.ingest(b)
    assert rb.status == "content_duplicate"
    assert docs_in_store(store) == 1
    assert pages_in_store(store) == 1


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


# ---------- shared image object store ----------

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes-A"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes-B"
JPG = b"\xff\xd8\xff" + b"fake-jpeg-bytes"


def test_put_image_dedups_identical_bytes(tmp_path):
    s = Store(tmp_path / "db", objects_dir=tmp_path / "obj")
    h1 = s.put_image(PNG)
    h2 = s.put_image(PNG)          # same bytes again
    assert h1 == h2
    # only one image row / one blob on disk
    assert s.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0] == 1
    blobs = list((tmp_path / "obj").rglob("*.png"))
    assert len(blobs) == 1
    s.close()


def test_image_shared_across_many_documents_stored_once(tmp_path):
    s = Store(tmp_path / "db", objects_dir=tmp_path / "obj")
    # Simulate 100 PDFs all using the same logo image.
    h = s.put_image(PNG)
    for i in range(100):
        page_hash = f"page{i}"
        # minimal page row so the FK on page_image is satisfiable
        s.conn.execute("INSERT INTO page (hash, text) VALUES (?, ?)", (page_hash, f"doc {i}"))
        s.link_page_images(page_hash, [h])
    s.conn.commit()
    assert s.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0] == 1   # one blob
    assert s.image_refcount(h) == 100                                        # referenced 100x
    assert len(list((tmp_path / "obj").rglob("*.png"))) == 1
    s.close()


def test_image_format_detected(tmp_path):
    s = Store(tmp_path / "db", objects_dir=tmp_path / "obj")
    hp = s.put_image(PNG)
    hj = s.put_image(JPG)
    fmt_png = s.conn.execute(
        "SELECT i.fmt FROM image_hash h JOIN image i ON i.id=h.image_id WHERE h.hash=?",
        (hp,)).fetchone()[0]
    fmt_jpg = s.conn.execute(
        "SELECT i.fmt FROM image_hash h JOIN image i ON i.id=h.image_id WHERE h.hash=?",
        (hj,)).fetchone()[0]
    assert fmt_png == 5 and fmt_jpg == 4
    s.close()


def test_reconstruct_returns_text_and_shared_image_paths(tmp_path):
    s = Store(tmp_path / "db", objects_dir=tmp_path / "obj")
    img = s.put_image(PNG)
    # Two 1-slide PDFs sharing the same image; reconstruct each.
    for name, fh, notes in [("a.pdf", "a" * 64, "page A text"),
                            ("b.pdf", "b" * 64, "page B text")]:
        page = Page(text=notes, image_hashes=[img])
        s.ingest(DocumentInput(full_hash=fh, file_size=1, source_type=SOURCE_PDF,
                               pages=[page], source_name=name))
        s.link_page_images(page.hash, [img])

    doc_id = s.conn.execute(
        "SELECT id FROM document WHERE source_name='a.pdf'").fetchone()[0]
    rebuilt = s.reconstruct(doc_id)
    assert len(rebuilt) == 1
    assert rebuilt[0]["text"] == "page A text"
    assert rebuilt[0]["images"][0].endswith(".png")
    # the referenced blob actually exists on disk
    assert Path(rebuilt[0]["images"][0]).exists()
    s.close()

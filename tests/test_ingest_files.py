"""Integration tests for the extraction -> corpus store bridge (store.ingest_files).

Proves the two implementations are unified: slide_dedupe's PDF/PPTX extraction
feeds the persistent Store (content-addressed pages, shared image object store,
versioning), with one hashing convention.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("pypdf")
pytest.importorskip("pptx")
pytest.importorskip("reportlab")
pytest.importorskip("PIL")

from store import Store, SOURCE_PDF, SOURCE_PPTX  # noqa: E402
from store.ingest_files import ingest_file  # noqa: E402


def make_pdf(path: Path, pages: list[str]) -> Path:
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i, text in enumerate(pages, 1):
        c.drawString(100, 700, text)
        c.drawString(500, 50, str(i))          # page-number footer (should be normalized away)
        c.showPage()
    c.save()
    return path


def make_image(path: Path, color) -> Path:
    from PIL import Image
    Image.new("RGB", (64, 48), color).save(path, "PNG")
    return path


def make_pptx_with_image(path: Path, img_path: Path, notes_per_slide: list[str]) -> Path:
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    for notes in notes_per_slide:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_picture(str(img_path), Inches(1), Inches(1))
        slide.notes_slide.notes_text_frame.text = notes
    prs.save(str(path))
    return path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db", objects_dir=tmp_path / "obj")
    yield s
    s.close()


def test_pdf_is_extracted_and_ingested(store, tmp_path):
    pdf = make_pdf(tmp_path / "report.pdf", ["intro text", "body text", "conclusion"])
    r = ingest_file(store, pdf)
    assert r.status == "stored"
    assert store.conn.execute("SELECT source_type FROM document WHERE id=?",
                              (r.document_id,)).fetchone()[0] == SOURCE_PDF
    # 3 pages extracted and stored
    assert store.conn.execute("SELECT page_count FROM document WHERE id=?",
                              (r.document_id,)).fetchone()[0] == 3


def test_same_pdf_reingested_is_exact_duplicate(store, tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", ["a", "b"])
    assert ingest_file(store, pdf).status == "stored"
    assert ingest_file(store, pdf).status == "duplicate"   # identical file bytes


def test_pptx_image_shared_across_slides_stored_once(store, tmp_path):
    img = make_image(tmp_path / "logo.png", (200, 30, 30))
    deck = make_pptx_with_image(tmp_path / "deck.pptx", img,
                                ["notes for slide 1", "notes for slide 2", "notes for slide 3"])
    r = ingest_file(store, deck)
    assert r.status == "stored"
    assert store.conn.execute("SELECT source_type FROM document WHERE id=?",
                              (r.document_id,)).fetchone()[0] == SOURCE_PPTX
    # All three slides embed the SAME picture -> stored once in the shared store.
    assert store.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0] == 1
    assert len(list((tmp_path / "obj").rglob("*.png"))) == 1


def test_pptx_same_picture_different_notes_flagged_as_visual_dup(store, tmp_path):
    img = make_image(tmp_path / "logo.png", (10, 10, 200))
    # Two decks, same single picture, different speaker notes.
    d1 = make_pptx_with_image(tmp_path / "v1.pptx", img, ["original notes"])
    d2 = make_pptx_with_image(tmp_path / "v2.pptx", img, ["rewritten notes"])
    ingest_file(store, d1)
    r2 = ingest_file(store, d2)
    assert r2.status == "stored"
    # same slide picture, reworded notes -> reported as a visual duplicate
    assert len(r2.visual_duplicates) == 1


def test_reconstruct_after_bridge_ingest(store, tmp_path):
    img = make_image(tmp_path / "pic.png", (0, 150, 0))
    deck = make_pptx_with_image(tmp_path / "deck.pptx", img, ["slide notes here"])
    r = ingest_file(store, deck)
    rebuilt = store.reconstruct(r.document_id)
    assert len(rebuilt) == 1
    # the single shared image is referenced and exists on disk
    assert rebuilt[0]["images"]
    assert Path(rebuilt[0]["images"][0]).exists()

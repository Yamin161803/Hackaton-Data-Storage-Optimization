"""Tests for node regeneration (store.regenerate) and the content-hash round-trip.

Proves that a document regenerated from the store is a valid PDF/PPTX and, when
re-ingested, is recognized as the same content (different bytes, same pages).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("reportlab")
pytest.importorskip("pptx")

from store import Store  # noqa: E402
from store.ingest_files import ingest_file  # noqa: E402
from store.regenerate import regenerate  # noqa: E402


def make_pdf(path, pages):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i, text in enumerate(pages, 1):
        c.drawString(100, 700, text); c.drawString(500, 50, str(i)); c.showPage()
    c.save()
    return path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db", objects_dir=tmp_path / "obj")
    yield s
    s.close()


def test_regenerate_pdf_is_valid(store, tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", ["alpha", "beta", "gamma"])
    r = ingest_file(store, pdf)
    pages = store.reconstruct(r.document_id)
    data, mime, ext = regenerate("pdf", pages)
    assert ext == "pdf" and mime == "application/pdf"
    assert data[:5] == b"%PDF-"


def test_regenerated_pdf_roundtrips_as_content_duplicate(store, tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", ["alpha", "beta", "gamma"])
    orig = ingest_file(store, pdf)

    # regenerate -> write to a new file (different bytes) -> re-ingest
    pages = store.reconstruct(orig.document_id)
    data, _, _ = regenerate("pdf", pages)
    regen = tmp_path / "regenerated.pdf"
    regen.write_bytes(data)

    r2 = ingest_file(store, regen)
    # different bytes, but same pages -> recognized by the content_hash gate
    assert r2.status == "content_duplicate"
    assert r2.document_id == orig.document_id


def test_regenerate_rejects_unknown_type():
    with pytest.raises(ValueError):
        regenerate("docx", [])

#!/usr/bin/env python3
"""Regenerate a PDF or PPTX from a stored document (content-faithful, not byte-identical).

Takes the output of Store.reconstruct(doc_id) -- an ordered list of
{position, text, images:[paths]} -- and rebuilds a downloadable file:

    * PDF  via reportlab: one page per stored page, its text drawn, its images placed.
    * PPTX via python-pptx: one slide per stored slide, speaker notes = stored text,
             images placed on the slide.

The result has the same CONTENT (text + the single stored copy of each picture,
in order) but different bytes than the original upload. Because the corpus store
identifies documents by content_hash (hash of the ordered page hashes), a
regenerated file re-uploaded later is still recognized as the same content.
"""
from __future__ import annotations

import io
from pathlib import Path


def regenerate_pdf(pages: list[dict]) -> bytes:
    """Rebuild a PDF from reconstruct() output. Returns the file bytes."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    for pg in pages:
        # images first (as a background/top area), then text below
        y_img_bottom = height
        for img_path in pg.get("images", []):
            p = Path(img_path)
            if not p.exists():
                continue
            try:
                img = ImageReader(str(p))
                iw, ih = img.getSize()
                scale = min((width - 100) / iw, (height / 2) / ih, 1.0)
                w, h = iw * scale, ih * scale
                y_img_bottom = height - 50 - h
                c.drawImage(img, 50, y_img_bottom, width=w, height=h,
                            preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
        text = (pg.get("text") or "").strip()
        if text:
            t = c.beginText(50, min(y_img_bottom - 20, height - 60))
            for line in text.splitlines():
                t.textLine(line[:120])
            c.drawText(t)
        c.showPage()
    c.save()
    return buf.getvalue()


def regenerate_pptx(pages: list[dict]) -> bytes:
    """Rebuild a PPTX from reconstruct() output (text -> speaker notes). Returns bytes."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    for pg in pages:
        slide = prs.slides.add_slide(blank)
        for i, img_path in enumerate(pg.get("images", [])):
            p = Path(img_path)
            if not p.exists():
                continue
            try:
                slide.shapes.add_picture(str(p), Inches(1), Inches(1 + i * 0.25),
                                         height=Inches(3))
            except Exception:
                pass
        # stored text is the speaker notes (that is the PPTX text identity)
        text = pg.get("text") or ""
        if text:
            slide.notes_slide.notes_text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def regenerate(source_type: str, pages: list[dict]) -> tuple[bytes, str, str]:
    """Return (bytes, mimetype, extension) for the given source type ('pdf'|'pptx')."""
    if source_type == "pdf":
        return regenerate_pdf(pages), "application/pdf", "pdf"
    if source_type == "pptx":
        return (regenerate_pptx(pages),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "pptx")
    raise ValueError(f"Cannot regenerate source type: {source_type}")

#!/usr/bin/env python3
"""Bridge: extract PDF / PPTX / image files and ingest them into the corpus Store.

This unifies the two implementations:
  * src/dedupe/slide_dedupe.py  -- extraction logic + file-level dedup/reporting.
  * src/store/                  -- persistent content-addressed storage, page/slide
                                   versioning, FTS search, shared image object store.

slide_dedupe's extractors yield (text, [image_hash, ...]) and throw the image
bytes away (it only needs hashes to decide which files to keep). The Store's
shared image object store needs the actual bytes to store each picture once.
So here we re-run the same extraction but KEEP the blobs, then:

    bytes -> Store.put_image()   (dedup: one copy per unique picture, across all docs)
    (text, [image_hash]) -> store.Page -> Store.ingest(DocumentInput)

Text + image hashing go through the Store helpers, which reuse
slide_dedupe.normalize_text, so both code paths agree on slide identity.

    python -m store.ingest_files database report.pdf deck.pptx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from store import Store, DocumentInput, Page, SOURCE_PDF, SOURCE_PPTX
from store.store import hash_images

# Reuse slide_dedupe's constants/extraction conventions.
from dedupe.slide_dedupe import IMAGE_EXTS, _iter_shapes, _raw_pdf_images, sha256
from videohash import fingerprint


# ---- bytes-returning extractors (same logic as slide_dedupe, but keep blobs) ----

def _pdf_pages_with_bytes(path: Path, use_images: bool = True):
    """Yield (text, [(image_hash, image_bytes), ...]) per PDF page."""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    for page in reader.pages:
        text = page.extract_text() or ""
        imgs = []
        if use_images:
            try:
                for img in page.images:            # decoded, re-encoded bytes
                    imgs.append((sha256(img.data), img.data))
            except Exception:
                imgs = []
        yield text, imgs


def _pptx_slides_with_bytes(path: Path, use_images: bool = True, notes_as_text: bool = True):
    """Yield (text, [(image_hash, image_bytes), ...]) per PPTX slide.

    For the corpus store the slide 'text' is the SPEAKER NOTES (notes_as_text),
    matching the store's PPTX identity (picture vs notes). Set notes_as_text
    False to use on-slide text instead.
    """
    from pptx import Presentation
    prs = Presentation(str(path))
    for slide in prs.slides:
        if notes_as_text:
            text = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                text = slide.notes_slide.notes_text_frame.text or ""
        else:
            parts = []
            for shape in _iter_shapes(slide.shapes):
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
            text = "\n".join(parts)
        imgs = []
        if use_images:
            for shape in _iter_shapes(slide.shapes):
                try:
                    blob = shape.image.blob
                    imgs.append((sha256(blob), blob))
                except (AttributeError, ValueError):
                    pass
        yield text, imgs


def _image_file_with_bytes(path: Path):
    """An image file = one slide: no text, the file's own bytes as its picture."""
    data = path.read_bytes()
    yield "", [(sha256(data), data)]


def _extract(path: Path, use_images: bool = True, notes_as_text: bool = True):
    ext = path.suffix.lower()
    if ext == ".pdf":
        return SOURCE_PDF, _pdf_pages_with_bytes(path, use_images)
    if ext == ".pptx":
        return SOURCE_PPTX, _pptx_slides_with_bytes(path, use_images, notes_as_text)
    if ext in IMAGE_EXTS:
        return SOURCE_PDF, _image_file_with_bytes(path)   # treated like a 1-slide doc
    raise ValueError(f"Unsupported file type: {path}")


# ---- ingest a file into the store ----

def ingest_file(store: Store, path: Path, use_images: bool = True,
                notes_as_text: bool = True):
    """Extract a file and ingest it into the Store.

    Images are stored once in the shared object store (dedup across all docs);
    pages are deduped; the document is version-tracked. Returns the IngestResult.
    """
    path = Path(path)
    source_type, slides = _extract(path, use_images, notes_as_text)

    pages: list[Page] = []
    page_image_links: list[tuple[int, list[str]]] = []  # (page index, image hex hashes)
    for text, imgs in slides:
        image_hashes = []
        for img_hash, img_bytes in imgs:
            stored_hash = store.put_image(img_bytes)      # dedup: one copy per picture
            image_hashes.append(stored_hash)
        pages.append(Page(text=text, image_hashes=image_hashes))
        page_image_links.append((len(pages) - 1, image_hashes))

    fp = fingerprint(path)
    doc = DocumentInput(
        full_hash=fp.full_hash,
        quick_hash=fp.quick_hash,
        file_size=fp.file_size,
        source_type=source_type,
        pages=pages,
        source_name=path.name,
    )
    result = store.ingest(doc)

    # Record page -> image references (for reconstruction) only if newly stored.
    if result.status == "stored":
        for idx, image_hashes in page_image_links:
            if image_hashes:
                store.link_page_images(pages[idx].hash, image_hashes)
        # Build + save the pixel-identical restore skeleton (images -> shared
        # ImageStore; skeleton -> document_skeleton). Best-effort: a skeleton
        # failure must not break ingest.
        _save_skeleton(store, result.document_id, path, source_type)
    return result


def _save_skeleton(store, document_id, path: Path, source_type) -> None:
    """Create and persist the exact-restore skeleton for a PDF/PPTX document."""
    import json
    ext = path.suffix.lower()
    images = store.image_store()
    try:
        if ext == ".pdf":
            from store.pdf_skeleton import store_pdf
            blob, skel = store_pdf(path, images)
            store.save_skeleton(document_id, "pdf", json.dumps(skel), blob)
        elif ext == ".pptx":
            from store.pptx_skeleton import store_pptx
            skel = store_pptx(path, images)
            store.save_skeleton(document_id, "pptx", json.dumps(skel), None)
    except Exception:
        pass   # dedup/versioning still works even if exact-restore is unavailable


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Extract PDF/PPTX/image files into the corpus store.")
    p.add_argument("db", help="path to the store database file")
    p.add_argument("files", nargs="+", help="PDF/PPTX/image files to ingest")
    p.add_argument("--no-images", action="store_true", help="skip image extraction")
    p.add_argument("--slide-text", action="store_true",
                   help="use on-slide text instead of speaker notes for PPTX identity")
    args = p.parse_args(argv)

    store = Store(args.db)
    try:
        for f in args.files:
            try:
                r = ingest_file(store, Path(f), use_images=not args.no_images,
                                notes_as_text=not args.slide_text)
            except (FileNotFoundError, ValueError) as exc:
                print(f"skip {f}: {exc}", file=sys.stderr)
                continue
            if r.status == "duplicate":
                print(f"{f}: exact duplicate of doc {r.document_id} (discarded)")
            else:
                extra = f", {len(r.visual_duplicates)} reworded slide(s)" if r.visual_duplicates else ""
                print(f"{f}: stored doc {r.document_id} "
                      f"({r.new_pages} new / {r.shared_pages} shared pages{extra})")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

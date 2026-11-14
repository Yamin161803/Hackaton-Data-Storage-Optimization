#!/usr/bin/env python3
"""Pixel-identical PDF storage by in-place image-stream substitution.

Keep the ORIGINAL PDF; pull each image XObject's raw stream bytes out into the
shared ImageStore (deduped + lossless). The "skeleton" is the original PDF with
those streams blanked, plus a map {objgen -> image hash + original /Filter}.
Restore writes the EXACT bytes back with the original filter -> pixel-identical.

Images are found by walking page Resources /XObject and following each image's
/SMask (soft mask), which is itself an image stream that must be restored too.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pikepdf

from .image_store import ImageStore

_PLACEHOLDER = b""


def _filter_name(obj) -> str | None:
    filt = obj.get("/Filter")
    if filt is None:
        return None
    try:
        return str(filt[0]) if isinstance(filt, pikepdf.Array) else str(filt)
    except Exception:
        return None


def _iter_image_xobjects(pdf):
    """Yield each unique image XObject (incl. nested /SMask), once per objgen."""
    seen = set()

    def visit(obj):
        try:
            if obj.get("/Subtype") != pikepdf.Name("/Image"):
                return
            key = tuple(obj.objgen)
        except Exception:
            return
        if key in seen:
            return
        seen.add(key)
        yield_list.append(obj)
        smask = obj.get("/SMask")               # soft mask is another image stream
        if smask is not None:
            visit(smask)

    yield_list = []
    for page in pdf.pages:
        res = page.get("/Resources")
        if res is None or "/XObject" not in res:
            continue
        xobjects = res["/XObject"]
        for name in list(xobjects.keys()):
            visit(xobjects[name])
    return yield_list


def store_pdf(path: str | Path, images: ImageStore) -> tuple[bytes, dict]:
    path = Path(path)
    original = path.read_bytes()
    full_hash = hashlib.sha256(original).hexdigest()

    pdf = pikepdf.open(io.BytesIO(original))
    img_entries = []
    for idx, obj in enumerate(_iter_image_xobjects(pdf)):
        try:
            raw = obj.read_raw_bytes()
        except Exception:
            continue
        filter_name = _filter_name(obj)         # BEFORE blanking
        h = images.put(raw).hash
        img_entries.append({
            "index": idx, "hash": h, "size": len(raw),
            "filter_name": filter_name,
        })
        obj.write(_PLACEHOLDER)                  # blank; restore puts it back

    skel_buf = io.BytesIO()
    pdf.save(skel_buf)
    pdf.close()
    return skel_buf.getvalue(), {"type": "pdf", "full_hash": full_hash, "images": img_entries}


def restore_pdf(skeleton_pdf_bytes: bytes, skeleton: dict, images: ImageStore) -> bytes:
    pdf = pikepdf.open(io.BytesIO(skeleton_pdf_bytes))
    # pikepdf renumbers objects on save, so match by WALK ORDER, not objgen.
    want = {e["index"]: e for e in skeleton["images"]}
    for idx, obj in enumerate(_iter_image_xobjects(pdf)):
        entry = want.get(idx)
        if entry is None:
            continue
        raw = images.get_original(entry["hash"])
        fname = entry.get("filter_name")
        if fname:
            obj.write(raw, filter=pikepdf.Name(fname))
        else:
            obj.write(raw)
    out = io.BytesIO()
    pdf.save(out)
    pdf.close()
    return out.getvalue()


def image_hashes(skeleton: dict) -> list[str]:
    return [e["hash"] for e in skeleton["images"]]

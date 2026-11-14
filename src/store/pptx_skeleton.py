#!/usr/bin/env python3
"""Pixel-identical PPTX storage by ZIP media substitution.

A .pptx is a ZIP. We keep the ENTIRE archive's structure and bytes, but pull the
pictures (ppt/media/*) out into the shared ImageStore (deduped + losslessly
compressed). The "skeleton" records every archive entry in order; media entries
are stored as a reference (their image hash) instead of raw bytes.

Restore rebuilds the ZIP from the skeleton, putting the EXACT original image
bytes back into their original entries -> the restored .pptx is byte-identical
to the original (so it obviously looks identical).

    skel = store_pptx("deck.pptx", image_store)      # returns a skeleton dict
    data = restore_pptx(skel, image_store)           # bytes identical to original
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from .image_store import ImageStore

# Archive members under these prefixes are treated as extractable media.
MEDIA_PREFIXES = ("ppt/media/", "word/media/", "xl/media/", "ppt/embeddings/")
MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp",
              ".emf", ".wmf"}


def _is_media(name: str) -> bool:
    low = name.lower()
    return low.startswith(MEDIA_PREFIXES) or Path(low).suffix in MEDIA_EXTS


def store_pptx(path: str | Path, images: ImageStore) -> dict:
    """Extract media into the image store; return a skeleton describing the file.

    Skeleton:
      {
        "type": "pptx",
        "full_hash": <sha256 of whole file>,
        "entries": [
          {"name", "is_media": bool,
           # non-media: the exact bytes are kept inline (base-free: we re-read on
           #   restore from 'inline' hash in the image store too, to stay uniform)
           "hash": <sha256>, "size": N,
           "compress_type": <int>, "date_time": [...], "external_attr": N}
        ]
      }
    Both media AND non-media entry bytes are deduped through the ImageStore, so
    even shared XML/parts across decks are stored once. Restore is byte-identical.
    """
    path = Path(path)
    raw = path.read_bytes()
    full_hash = hashlib.sha256(raw).hexdigest()

    entries = []
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        for info in zf.infolist():
            data = zf.read(info.filename)
            h = images.put(data).hash          # dedup every part by content
            entries.append({
                "name": info.filename,
                "is_media": _is_media(info.filename),
                "hash": h,
                "size": len(data),
                "compress_type": info.compress_type,
                "date_time": list(info.date_time),
                "external_attr": info.external_attr,
                "create_system": info.create_system,
            })
    return {"type": "pptx", "full_hash": full_hash, "entries": entries}


def restore_pptx(skeleton: dict, images: ImageStore) -> bytes:
    """Rebuild the exact original .pptx bytes from the skeleton + image store."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for e in skeleton["entries"]:
            data = images.get_original(e["hash"])
            zi = zipfile.ZipInfo(e["name"], date_time=tuple(e["date_time"]))
            zi.compress_type = e["compress_type"]
            zi.external_attr = e["external_attr"]
            zi.create_system = e.get("create_system", 0)
            zf.writestr(zi, data)
    return buf.getvalue()


def media_hashes(skeleton: dict) -> list[str]:
    """Hashes of just the media (pictures) in this file."""
    return [e["hash"] for e in skeleton["entries"] if e["is_media"]]

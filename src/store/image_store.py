#!/usr/bin/env python3
"""Shared, content-addressed image store with LOSSLESS compression.

Pictures pulled out of PDFs and slide decks are stored here ONCE, keyed by the
SHA-256 of their ORIGINAL bytes. The same picture used by 100 files costs one
blob on disk. Blobs are optionally recompressed losslessly (so the exact same
pixels come back) and transparently restored to the original bytes on read.

Key guarantee: get_original(hash) returns bytes IDENTICAL to what was put in.
That is what makes PDF/PPTX restore pixel-identical.

Layout on disk (sharded so dirs stay small at scale):
    <root>/ab/cd/<sha256>.blob      # stored image (maybe recompressed)
    <root>/ab/cd/<sha256>.meta      # how to restore the exact original bytes
"""
from __future__ import annotations

import hashlib
import json
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StoredImage:
    hash: str
    original_size: int
    stored_size: int
    codec: str          # how the blob is encoded: "raw" | "zlib"


class ImageStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths --
    def _paths(self, h: str) -> tuple[Path, Path]:
        d = self.root / h[:2] / h[2:4]
        return d / f"{h}.blob", d / f"{h}.meta"

    def has(self, h: str) -> bool:
        blob, _ = self._paths(h)
        return blob.exists()

    # -- write --
    def put(self, data: bytes) -> StoredImage:
        """Store image bytes once (deduped). Returns StoredImage with the hash.

        Lossless recompression: we zlib the original bytes; if that is smaller we
        keep the compressed form (codec=zlib), else store raw. Either way the
        ORIGINAL bytes are what we return on read, so restore is pixel-identical.
        """
        h = hashlib.sha256(data).hexdigest()
        blob, meta = self._paths(h)
        if blob.exists():                      # already stored -> dedup hit
            return self._read_meta(h)

        blob.parent.mkdir(parents=True, exist_ok=True)
        compressed = zlib.compress(data, level=9)
        if len(compressed) < len(data):
            blob.write_bytes(compressed)
            codec = "zlib"
            stored = len(compressed)
        else:
            blob.write_bytes(data)
            codec = "raw"
            stored = len(data)
        info = StoredImage(hash=h, original_size=len(data), stored_size=stored, codec=codec)
        meta.write_text(json.dumps({
            "hash": h, "original_size": info.original_size,
            "stored_size": info.stored_size, "codec": codec,
        }), encoding="utf-8")
        return info

    # -- read --
    def _read_meta(self, h: str) -> StoredImage:
        _, meta = self._paths(h)
        m = json.loads(meta.read_text(encoding="utf-8"))
        return StoredImage(h, m["original_size"], m["stored_size"], m["codec"])

    def get_original(self, h: str) -> bytes:
        """Return bytes IDENTICAL to what was put in (lossless round-trip)."""
        blob, meta = self._paths(h)
        raw = blob.read_bytes()
        codec = json.loads(meta.read_text(encoding="utf-8"))["codec"]
        data = zlib.decompress(raw) if codec == "zlib" else raw
        return data

    # -- stats --
    def stats(self) -> dict:
        blobs = list(self.root.rglob("*.blob"))
        stored = sum(b.stat().st_size for b in blobs)
        original = 0
        for m in self.root.rglob("*.meta"):
            try:
                original += json.loads(m.read_text())["original_size"]
            except Exception:
                pass
        return {"unique_images": len(blobs), "stored_bytes": stored,
                "original_bytes": original, "saved_bytes": max(0, original - stored)}

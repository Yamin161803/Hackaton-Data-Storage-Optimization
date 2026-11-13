"""
slide_dedupe.py

Find the smallest collection of PDF and PowerPoint files that preserves
all unique slides.

The script compares individual slides across files. Each slide is identified
by a SHA-256 hash calculated from:
    - Normalized text extracted from the slide.
    - SHA-256 hashes of images embedded in the slide.

Two slides are considered identical if their normalized text and image hashes
match. Page numbers are ignored by default.

A file is represented by the set of unique slides it contains. The script
keeps files that contribute slides not found in other kept files and discards
files that add no unique information.

PROCESSING MODES
----------------

online (default):
    Process files one at a time, in the order they are provided.
    A file is kept if it introduces at least one previously unseen slide.
    Previously kept files are removed from the collection if their slides
    become fully covered by other kept files.

    The index JSON stores the results between runs, allowing you to process
    additional files later without reprocessing the existing collection.

batch:
    Evaluate all new files and previously kept files together.
    Use greedy set cover to select files that collectively contain every
    unique slide, then remove redundant files.

    Usually produces a smaller collection than online mode. The result is
    not guaranteed to be mathematically optimal.

INPUTS
------

You can provide:
    - Individual PDF or PPTX files.
    - Multiple files in one command.
    - One or more folders.
    - Any combination of files and folders.

Folders are scanned recursively. Files are processed in sorted order within
each folder. Supported extensions are defined by SUPPORTED (.pdf, .pptx).

OPTIONS
-------

--index PATH
    Path to the JSON file used to store the collection's state.
    Default: slide_index.json

--mode {online,batch}
    Select the processing mode. Default: online.

--trash PATH
    Move discarded and evicted files into this folder instead of leaving
    them in their original locations.
    Without this option, files are never moved or deleted.

--text-only
    Ignore embedded images when comparing slides. Only extracted text is used.

--keep-page-numbers
    Include page-number lines when comparing slides.
    By default, lines such as "7", "Page 7", and "7 / 20" are ignored.

USAGE EXAMPLES
--------------

Process every supported file in a folder and its subfolders:
    python slide_dedupe.py data/

Process multiple folders:
    python slide_dedupe.py lectures/ presentations/ archive/

Process specific files:
    python slide_dedupe.py a.pdf b.pptx c.pdf

Combine individual files and folders:
    python slide_dedupe.py data/ extra.pdf backup/

Process a collection in batch mode:
    python slide_dedupe.py data/ --mode batch

Continue processing an existing collection:
    python slide_dedupe.py new_material/ --index slide_index.json

Use a separate index for another collection:
    python slide_dedupe.py data/ --index university_slides.json

Compare slides using text only, ignoring images:
    python slide_dedupe.py data/ --text-only

Include page numbers in slide comparisons:
    python slide_dedupe.py data/ --keep-page-numbers

Move discarded files into a separate folder:
    python slide_dedupe.py data/ --trash discarded/

Run batch processing and move discarded files:
    python slide_dedupe.py data/ --mode batch --trash discarded/

Combine options:
    python slide_dedupe.py data/ --mode batch --text-only --index archive.json --trash discarded/

REQUIREMENTS
------------

    pip install pypdf python-pptx

NOTES
-----

- Files are never moved or deleted unless --trash is specified.
- The index JSON stores slide hashes, not copies of the original files.
- Previously processed files are skipped on subsequent runs.
- If a previously processed file changes, remove its entry from the index
  or use a new index to make the script process it again.
- Image comparison uses the original embedded image data, not visual
  similarity. Visually identical images with different encodings may
  therefore produce different hashes.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

SUPPORTED = {".pdf", ".pptx"}

# Lines like "7", "Page 7", "7 / 20", "7 of 20" are slide numbers / footers.
# They change when a slide moves position, so by default they are ignored.
_PAGE_NUMBER = re.compile(r"(page|slide|side)?\s*\d+(\s*(/|of|af)\s*\d+)?", re.IGNORECASE)

# --------------------------------------------------------------------------
# Slide hashing
# --------------------------------------------------------------------------

def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str, drop_page_numbers: bool = True) -> str:
    """Collapse whitespace, drop empty lines and (optionally) page-number lines."""
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        if drop_page_numbers and _PAGE_NUMBER.fullmatch(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def slide_hash(text: str, image_hashes: list[str], drop_page_numbers: bool = True) -> str:
    """Hash of one slide = normalized text + sorted unique image hashes."""
    payload = (
        normalize_text(text, drop_page_numbers)
        + "\n\x00images\x00\n"
        + "\n".join(sorted(set(image_hashes)))
    )
    return sha256(payload.encode("utf-8"))


def pdf_slides(path: Path, use_images: bool = True):
    """Yield (text, [image_hash, ...]) for each page of a PDF."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    for page in reader.pages:
        text = page.extract_text() or ""
        images = []
        if use_images:
            try:
                images = [sha256(img.data) for img in page.images]
            except Exception:  # broken/unsupported image stream: fall back to text
                images = []
        yield text, images


def _iter_shapes(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(shape.shapes)
        else:
            yield shape


def pptx_slides(path: Path, use_images: bool = True):
    """Yield (text, [image_hash, ...]) for each slide of a PPTX."""
    from pptx import Presentation

    prs = Presentation(str(path))
    for slide in prs.slides:
        texts, images = [], []
        for shape in _iter_shapes(slide.shapes):
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        texts.append(cell.text)
            if use_images:
                try:
                    images.append(sha256(shape.image.blob))
                except (AttributeError, ValueError):
                    pass  # shape has no picture
        yield "\n".join(texts), images


def file_slide_hashes(path: Path, use_images: bool = True, drop_page_numbers: bool = True) -> list[str]:
    """Return the slide hashes of a file, in slide order (duplicates kept)."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        slides = pdf_slides(path, use_images)
    elif ext == ".pptx":
        slides = pptx_slides(path, use_images)
    else:
        raise ValueError(f"Unsupported file type: {path}")
    return [slide_hash(t, imgs, drop_page_numbers) for t, imgs in slides]


# --------------------------------------------------------------------------
# Keep / discard logic (pure, no file I/O, easy to test)
# --------------------------------------------------------------------------

class SlideIndex:
    """
    Tracks the kept files and how many kept files contain each slide hash.

    Invariant: the union of slides in kept files == every slide ever seen.
    """

    def __init__(self):
        self.kept: dict[str, set[str]] = {}
        self.coverage: Counter[str] = Counter()
        self.discarded: dict[str, str] = {}  # name -> reason

    # -- basic operations ------------------------------------------------
    def _keep(self, name: str, hashes: set[str]) -> None:
        self.kept[name] = set(hashes)
        self.coverage.update(self.kept[name])
        self.discarded.pop(name, None)

    def _drop(self, name: str, reason: str) -> None:
        for h in self.kept.pop(name):
            self.coverage[h] -= 1
            if self.coverage[h] == 0:
                del self.coverage[h]
        self.discarded[name] = reason

    def is_redundant(self, name: str) -> bool:
        """True if every slide of a kept file is also in another kept file."""
        return all(self.coverage[h] > 1 for h in self.kept[name])

    def prune(self) -> list[str]:
        """
        Evict kept files whose slides are all covered by other kept files.
        Smallest files are tried first. One pass is enough: evicting a file
        only lowers coverage, so it can never make another file redundant.
        """
        evicted = []
        for name in sorted(self.kept, key=lambda n: (len(self.kept[n]), n)):
            if self.is_redundant(name):
                self._drop(name, "evicted: all slides covered by other kept files")
                evicted.append(name)
        return evicted

    # -- online mode -----------------------------------------------------
    def add(self, name: str, hashes) -> tuple[bool, list[str]]:
        """
        Offer a file. Returns (kept, evicted_names).
        A file with no unseen slide is discarded; otherwise it is kept and
        any now-redundant older files are evicted.
        """
        hashes = set(hashes)
        new_slides = hashes - self.coverage.keys()
        if not new_slides:
            self.discarded[name] = "no new slides"
            return False, []
        self._keep(name, hashes)
        return True, self.prune()

    # -- batch mode ------------------------------------------------------
    @classmethod
    def plan_batch(cls, files: dict[str, set[str]]) -> "SlideIndex":
        """
        Greedy set cover: repeatedly keep the file that adds the most
        not-yet-covered slides (ties: bigger file, then name), then prune.
        """
        index = cls()
        candidates = {n: set(h) for n, h in sorted(files.items())}
        remaining = set().union(*candidates.values()) if candidates else set()
        while remaining:
            best = max(candidates, key=lambda n: (len(candidates[n] & remaining), len(candidates[n])))
            remaining -= candidates[best]
            index._keep(best, candidates.pop(best))
        index.prune()
        for name in candidates:
            index.discarded[name] = "no new slides"
        return index

    # -- reporting / persistence ------------------------------------------
    def covered_by(self, hashes) -> list[str]:
        """Kept files that share at least one slide with the given hashes."""
        hashes = set(hashes)
        return sorted(n for n, hs in self.kept.items() if hs & hashes)

    def to_json(self) -> dict:
        return {
            "kept": {n: sorted(hs) for n, hs in sorted(self.kept.items())},
            "discarded": dict(sorted(self.discarded.items())),
            "unique_slides": len(self.coverage),
        }

    @classmethod
    def from_json(cls, data: dict) -> "SlideIndex":
        index = cls()
        for name, hashes in data.get("kept", {}).items():
            index._keep(name, set(hashes))
        index.discarded.update(data.get("discarded", {}))
        return index


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def collect_files(inputs: list[str]) -> list[Path]:
    """Expand folders (recursively, sorted) and keep the given order of files."""
    files, seen = [], set()
    for item in inputs:
        p = Path(item)
        found = sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED) if p.is_dir() else [p]
        for f in found:
            key = str(f.resolve())
            if key not in seen and f.suffix.lower() in SUPPORTED:
                seen.add(key)
                files.append(f)
    return files


def move_to_trash(name: str, trash: Path) -> None:
    src = Path(name)
    if not src.exists():
        return
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / src.name
    i = 1
    while dest.exists():
        dest = trash / f"{src.stem}_{i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(dest))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Keep the fewest PDF/PPTX files that still hold every unique slide.")
    ap.add_argument("inputs", nargs="+", help="PDF/PPTX files and/or folders")
    ap.add_argument("--index", default="slide_index.json", help="State file (default: slide_index.json)")
    ap.add_argument("--mode", choices=["online", "batch"], default="online")
    ap.add_argument("--trash", help="Move discarded/evicted files into this folder")
    ap.add_argument("--text-only", action="store_true", help="Ignore images when hashing slides")
    ap.add_argument("--keep-page-numbers", action="store_true", help="Do not strip page-number lines")
    args = ap.parse_args(argv)

    index_path = Path(args.index)
    index = SlideIndex.from_json(json.loads(index_path.read_text("utf-8"))) if index_path.exists() else SlideIndex()
    previously_kept = set(index.kept)

    # Hash new files
    hashed: dict[str, set[str]] = {}
    for f in collect_files(args.inputs):
        name = str(f.resolve())
        if name in index.kept or name in index.discarded:
            continue  # already processed in an earlier run
        try:
            hashes = file_slide_hashes(f, use_images=not args.text_only,
                                       drop_page_numbers=not args.keep_page_numbers)
        except Exception as exc:
            print(f"ERROR  {f}: {exc}", file=sys.stderr)
            continue
        hashed[name] = set(hashes)
        print(f"hashed {f}  ({len(hashes)} slides, {len(set(hashes))} distinct)")

    # Decide
    if args.mode == "online":
        for name, hashes in hashed.items():
            kept, evicted = index.add(name, hashes)
            print(f"{'KEEP ' if kept else 'SKIP '} {name}")
            for e in evicted:
                print(f"EVICT  {e}")
    else:
        old_discarded = {n: r for n, r in index.discarded.items()}
        index = SlideIndex.plan_batch({**index.kept, **hashed})
        for n, r in old_discarded.items():
            index.discarded.setdefault(n, r)

    # Act
    to_remove = [n for n in index.discarded if n in hashed or n in previously_kept]
    if args.trash:
        for n in to_remove:
            move_to_trash(n, Path(args.trash))

    index_path.write_text(json.dumps(index.to_json(), indent=2), encoding="utf-8")

    print()
    print(f"Unique slides: {len(index.coverage)}")
    print(f"Kept files:    {len(index.kept)}")
    for n in sorted(index.kept):
        print(f"  + {n}")
    print(f"Not kept:      {len(to_remove)}{'  (moved to ' + args.trash + ')' if args.trash else '  (report only)'}")
    for n in to_remove:
        print(f"  - {n}  [{index.discarded[n]}]")
    print(f"Index saved:   {index_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


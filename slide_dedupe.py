"""
slide_dedupe.py

Find the smallest collection of PDF and PowerPoint files that preserves
all unique slides, and report how much disk space the cleanup saves and
where the run spends its time.

The script compares individual slides across files. Each slide is identified
by a SHA-256 hash calculated from:
    - Normalized text extracted from the slide.
    - SHA-256 hashes of images embedded in the slide.

Two slides are considered identical if their normalized text and image hashes
match. Page numbers are ignored by default.

A file is represented by the set of unique slides it contains. The script
keeps files that contribute slides not found in other kept files and discards
files that add no unique information.

PIPELINE
--------

1. scan       Expand folders into a list of PDF/PPTX files.
2. prefilter  Find byte-identical copies cheaply, before opening any file:
                a. file size     (free: one stat call)
                b. quick hash    (only for files whose size matches another
                                  file; reads 3 x 64 KB: start, middle, end)
                c. full hash     (only when the quick hash also matches;
                                  reads the whole file)
              A confirmed copy reuses the slides of the original and skips
              slide extraction, which is by far the slowest step.
3. extract    Open each remaining file and hash its slides
              (optionally in parallel with --workers).
4. decide     Keep / discard / evict files (online or batch mode).
5. act        Move dropped files to --trash (only when given) and save the index.

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
    Without this option, files are never moved or deleted, and the space
    report shows what you WOULD save.

--text-only
    Ignore embedded images when comparing slides. Only extracted text is used.

--keep-page-numbers
    Include page-number lines when comparing slides.
    By default, lines such as "7", "Page 7", and "7 / 20" are ignored.

--fast-images
    PDF only: hash the raw (still compressed) image streams instead of
    letting pypdf decode and re-encode every image. Much faster on
    image-heavy PDFs, but produces different hashes than the default, so
    use one setting consistently per index. Inline images are ignored.

--no-prefilter
    Skip the size / quick-hash / full-hash duplicate check.

--workers N
    Extract slides from N files in parallel (processes). Default: 1.
    Keep/discard decisions still follow the input order.

--report-json PATH
    Also write the space and performance report as JSON (for tracking runs
    over time or comparing settings).

--profile PATH
    Run under cProfile and save the profile to PATH (e.g. run.prof).
    Prints the top functions by own time. With --workers > 1 only the main
    process is profiled, so use --workers 1 when profiling.

--snakeviz
    After --profile, open the profile in snakeviz (pip install snakeviz).

--top N
    Number of slowest files to list. Default: 10.

USAGE EXAMPLES
--------------

Dry run with space and timing report:
    python slide_dedupe.py data/

Find bottlenecks with cProfile + snakeviz:
    python slide_dedupe.py data/ --index /tmp/test_index.json --profile run.prof --snakeviz

Faster run on a big folder:
    python slide_dedupe.py data/ --workers 4 --fast-images

Track runs over time:
    python slide_dedupe.py data/ --report-json reports/run_2026-11-13.json

Move discarded files:
    python slide_dedupe.py data/ --mode batch --trash discarded/

REQUIREMENTS
------------

    pip install pypdf python-pptx snakeviz
    pip install snakeviz          # optional, for --snakeviz

NOTES
-----

- Files are never moved or deleted unless --trash is specified.
- Moving files into --trash frees space in your source folders, but the
  disk space is only reclaimed once the trash folder is deleted.
- The index JSON stores slide hashes and file sizes/hashes, not copies of
  the original files.
- Previously processed files are skipped on subsequent runs.
- If a previously processed file changes, remove its entry from the index
  or use a new index to make the script process it again.
- Image comparison uses embedded image data, not visual similarity.
  Visually identical images with different encodings may therefore
  produce different hashes.
- The prefilter only catches byte-identical files. A re-saved or
  re-exported copy is still caught, just later, by the slide comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

SUPPORTED = {".pdf", ".pptx"}

# Lines like "7", "Page 7", "7 / 20", "7 of 20" are slide numbers / footers.
# They change when a slide moves position, so by default they are ignored.
_PAGE_NUMBER = re.compile(r"(page|slide|side)?\s*\d+(\s*(/|of|af)\s*\d+)?", re.IGNORECASE)

QUICK_CHUNK = 64 * 1024        # bytes read per sample for the quick hash
FULL_CHUNK = 1024 * 1024       # read size for the full hash


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

class Stats:
    """
    Accumulates time, call counts and bytes per named phase.

    with stats.phase("pdf.text"):
        ...
    """

    def __init__(self):
        self.seconds: defaultdict[str, float] = defaultdict(float)
        self.calls: Counter[str] = Counter()
        self.bytes: Counter[str] = Counter()

    @contextmanager
    def phase(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += time.perf_counter() - t0
            self.calls[name] += 1

    def add(self, name: str, seconds: float, calls: int = 1, n_bytes: int = 0) -> None:
        self.seconds[name] += seconds
        self.calls[name] += calls
        if n_bytes:
            self.bytes[name] += n_bytes

    def to_dict(self) -> dict:
        return {
            n: {"seconds": self.seconds[n], "calls": self.calls[n], "bytes": self.bytes[n]}
            for n in self.seconds
        }

    def merge(self, data: dict) -> None:
        for n, v in data.items():
            self.add(n, v["seconds"], v["calls"], v.get("bytes", 0))


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


def _raw_pdf_images(page) -> list[bytes]:
    """Raw (still encoded) bytes of every image XObject on a page, incl. inside forms."""
    out, seen = [], set()

    def walk(resources):
        if not resources:
            return
        xobjects = resources.get_object().get("/XObject")
        if not xobjects:
            return
        for ref in xobjects.get_object().values():
            obj = ref.get_object()
            key = id(obj)
            if key in seen:
                continue
            seen.add(key)
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                out.append(obj._data)
            elif subtype == "/Form":
                walk(obj.get("/Resources"))

    walk(page.get("/Resources"))
    return out


def pdf_slides(path: Path, use_images: bool = True, stats: Stats | None = None,
               fast_images: bool = False):
    """Yield (text, [image_hash, ...]) for each page of a PDF."""
    stats = stats or Stats()
    with stats.phase("pdf.open"):
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = reader.pages
    for page in pages:
        with stats.phase("pdf.text"):
            text = page.extract_text() or ""
        images = []
        if use_images:
            with stats.phase("pdf.images.fast" if fast_images else "pdf.images"):
                try:
                    if fast_images:
                        blobs = _raw_pdf_images(page)
                    else:
                        blobs = [img.data for img in page.images]  # decodes + re-encodes
                    images = [sha256(b) for b in blobs]
                    stats.bytes["pdf.images.fast" if fast_images else "pdf.images"] += sum(map(len, blobs))
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


def pptx_slides(path: Path, use_images: bool = True, stats: Stats | None = None):
    """Yield (text, [image_hash, ...]) for each slide of a PPTX."""
    stats = stats or Stats()
    with stats.phase("pptx.open"):
        from pptx import Presentation
        prs = Presentation(str(path))
    for slide in prs.slides:
        texts, images = [], []
        img_time, img_bytes = 0.0, 0
        t0 = time.perf_counter()
        for shape in _iter_shapes(slide.shapes):
            if shape.has_text_frame:
                texts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        texts.append(cell.text)
            if use_images:
                ti = time.perf_counter()
                try:
                    blob = shape.image.blob
                    images.append(sha256(blob))
                    img_bytes += len(blob)
                except (AttributeError, ValueError):
                    pass  # shape has no picture
                img_time += time.perf_counter() - ti
        total = time.perf_counter() - t0
        stats.add("pptx.text", total - img_time)
        if use_images:
            stats.add("pptx.images", img_time, n_bytes=img_bytes)
        yield "\n".join(texts), images


def file_slide_hashes(path: Path, use_images: bool = True, drop_page_numbers: bool = True,
                      stats: Stats | None = None, fast_images: bool = False) -> list[str]:
    """Return the slide hashes of a file, in slide order (duplicates kept)."""
    stats = stats or Stats()
    ext = path.suffix.lower()
    if ext == ".pdf":
        slides = pdf_slides(path, use_images, stats, fast_images)
    elif ext == ".pptx":
        slides = pptx_slides(path, use_images, stats)
    else:
        raise ValueError(f"Unsupported file type: {path}")
    out = []
    for t, imgs in slides:
        with stats.phase("slide.hash"):
            out.append(slide_hash(t, imgs, drop_page_numbers))
    return out


def _extract_worker(job):
    """Top-level so it can run in a worker process. Never raises."""
    name, use_images, drop_page_numbers, fast_images = job
    stats = Stats()
    t0 = time.perf_counter()
    try:
        hashes = file_slide_hashes(Path(name), use_images, drop_page_numbers, stats, fast_images)
        err = None
    except Exception as exc:
        hashes, err = [], f"{type(exc).__name__}: {exc}"
    return name, hashes, time.perf_counter() - t0, stats.to_dict(), err


# --------------------------------------------------------------------------
# Prefilter: size -> quick hash -> full hash
# --------------------------------------------------------------------------

def quick_hash(path: Path, size: int) -> str:
    """Hash of size + first, middle and last 64 KB. Whole file if it is small."""
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        if size <= 3 * QUICK_CHUNK:
            h.update(f.read())
        else:
            for offset in (0, size // 2 - QUICK_CHUNK // 2, size - QUICK_CHUNK):
                f.seek(offset)
                h.update(f.read(QUICK_CHUNK))
    return h.hexdigest()


def full_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(FULL_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class ContentMatcher:
    """
    Finds byte-identical files with as little reading as possible.

    Every file is registered with its size. The quick hash is computed only
    when two files share a size, and the full hash only when the quick hashes
    also match. Hashes are cached in `records` (which is saved in the index),
    so later runs can match against earlier files without re-reading them.
    """

    def __init__(self, records: dict[str, dict] | None = None, stats: Stats | None = None):
        self.records: dict[str, dict] = records if records is not None else {}
        self.stats = stats or Stats()
        self.by_size: defaultdict[int, list[str]] = defaultdict(list)
        for name, rec in self.records.items():
            if "size" in rec:
                self.by_size[rec["size"]].append(name)
        self.counts = Counter()

    def _ensure(self, name: str, kind: str) -> str | None:
        rec = self.records[name]
        if kind in rec:
            return rec[kind]
        p = Path(name)
        try:
            if p.stat().st_size != rec["size"]:
                return None          # file changed or replaced since it was indexed
            with self.stats.phase(f"prefilter.{kind}_hash"):
                if kind == "quick":
                    value = quick_hash(p, rec["size"])
                    self.stats.bytes["prefilter.quick_hash"] += min(rec["size"], 3 * QUICK_CHUNK)
                else:
                    value = full_hash(p)
                    self.stats.bytes["prefilter.full_hash"] += rec["size"]
        except OSError:
            return None              # file was moved / deleted
        self.counts[f"{kind}_hashed"] += 1
        rec[kind] = value
        return value

    def register(self, name: str, size: int | None = None) -> str | None:
        """
        Register a file. Returns the name of an earlier byte-identical file,
        or None. `size` is looked up with os.stat when not given.
        """
        with self.stats.phase("prefilter.size"):
            if size is None:
                size = os.stat(name).st_size
            same_size = [n for n in self.by_size[size] if n != name]
        self.records.setdefault(name, {})["size"] = size
        if name not in self.by_size[size]:
            self.by_size[size].append(name)
        if not same_size:
            self.counts["unique_size"] += 1
            return None
        self.counts["size_match"] += 1
        my_quick = self._ensure(name, "quick")
        for other in same_size:
            if my_quick is None or self._ensure(other, "quick") != my_quick:
                continue
            self.counts["quick_match"] += 1
            my_full = self._ensure(name, "full")
            if my_full is not None and self._ensure(other, "full") == my_full:
                self.counts["duplicates"] += 1
                return other
        return None


# --------------------------------------------------------------------------
# Keep / discard logic (pure, no file I/O, easy to test)
# --------------------------------------------------------------------------

class SlideIndex:
    """
    Tracks the kept files and how many kept files contain each slide hash.

    Invariant: the union of slides in kept files == every slide ever seen.
    """

    def __init__(self, stats: Stats | None = None):
        self.kept: dict[str, set[str]] = {}
        self.coverage: Counter[str] = Counter()
        self.discarded: dict[str, str] = {}  # name -> reason
        self.files: dict[str, dict] = {}     # name -> {size, quick, full}
        self.settings: dict = {}
        self.stats = stats or Stats()

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
        with self.stats.phase("compare.prune"):
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
        with self.stats.phase("compare.new_slides"):
            hashes = set(hashes)
            new_slides = hashes - self.coverage.keys()
        if not new_slides:
            self.discarded[name] = "no new slides"
            return False, []
        self._keep(name, hashes)
        return True, self.prune()

    # -- batch mode ------------------------------------------------------
    @classmethod
    def plan_batch(cls, files: dict[str, set[str]], stats: Stats | None = None) -> "SlideIndex":
        """
        Greedy set cover: repeatedly keep the file that adds the most
        not-yet-covered slides (ties: bigger file, then name), then prune.
        """
        index = cls(stats)
        with index.stats.phase("compare.set_cover"):
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
            "settings": self.settings,
            "kept": {n: sorted(hs) for n, hs in sorted(self.kept.items())},
            "discarded": dict(sorted(self.discarded.items())),
            "files": dict(sorted(self.files.items())),
            "unique_slides": len(self.coverage),
        }

    @classmethod
    def from_json(cls, data: dict, stats: Stats | None = None) -> "SlideIndex":
        index = cls(stats)
        for name, hashes in data.get("kept", {}).items():
            index._keep(name, set(hashes))
        index.discarded.update(data.get("discarded", {}))
        index.files.update(data.get("files", {}))
        index.settings = data.get("settings", {})
        return index


# --------------------------------------------------------------------------
# Profiling
# --------------------------------------------------------------------------

def speedtest(function, filename: str = "speedtest_profile.prof", open_snakeviz: bool = False,
              top: int = 25):
    """
    Run `function()` under cProfile, save the profile, print the hottest
    functions by own time and optionally open it in snakeviz.
    Returns whatever `function` returns.
    """
    import cProfile
    import pstats

    with cProfile.Profile() as pr:
        result = function()
    pr.dump_stats(filename)
    print(f"\nProfile saved: {filename}   (view: snakeviz {filename})")
    pstats.Stats(pr).sort_stats(pstats.SortKey.TIME).print_stats(top)
    if open_snakeviz:
        try:
            import snakeviz.cli as cli
        except ImportError:
            print("snakeviz is not installed: pip install snakeviz", file=sys.stderr)
        else:
            cli.main([filename])  # starts a local web server; Ctrl+C to stop
    return result


# --------------------------------------------------------------------------
# Reporting helpers
# --------------------------------------------------------------------------

def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _size_of(name: str, index: SlideIndex) -> int:
    rec = index.files.get(name, {})
    if "size" in rec:
        return rec["size"]
    try:
        return os.stat(name).st_size
    except OSError:
        return 0


def _bottleneck_hints(stats: Stats, settings: dict) -> list[str]:
    extract = {n: s for n, s in stats.seconds.items() if n.split(".")[0] in ("pdf", "pptx", "slide")}
    total = sum(extract.values())
    if total <= 0:
        return []
    name, secs = max(extract.items(), key=lambda kv: kv[1])
    share = secs / total
    hints = [f"Biggest cost: {name} ({share:.0%} of slide extraction time)."]
    if name == "pdf.images" and share > 0.3:
        hints.append("Try --fast-images (hash compressed image data) or --text-only.")
    elif name == "pdf.text" and share > 0.5:
        hints.append("PDF text extraction dominates; --workers N runs files in parallel.")
    elif name.startswith("pptx") and share > 0.5:
        hints.append("PowerPoint parsing dominates; --workers N runs files in parallel.")
    if settings.get("workers", 1) == 1 and total > 5:
        hints.append(f"Extraction took {total:.1f}s on 1 worker; try --workers {max(2, (os.cpu_count() or 2) - 1)}.")
    return hints


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


def move_to_trash(name: str, trash: Path) -> bool:
    src = Path(name)
    if not src.exists():
        return False
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / src.name
    i = 1
    while dest.exists():
        dest = trash / f"{src.stem}_{i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(dest))
    return True


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Keep the fewest PDF/PPTX files that still hold every unique slide.")
    ap.add_argument("inputs", nargs="+", help="PDF/PPTX files and/or folders")
    ap.add_argument("--index", default="slide_index.json", help="State file (default: slide_index.json)")
    ap.add_argument("--mode", choices=["online", "batch"], default="online")
    ap.add_argument("--trash", help="Move discarded/evicted files into this folder")
    ap.add_argument("--text-only", action="store_true", help="Ignore images when hashing slides")
    ap.add_argument("--keep-page-numbers", action="store_true", help="Do not strip page-number lines")
    ap.add_argument("--fast-images", action="store_true", help="PDF: hash compressed image data (faster, different hashes)")
    ap.add_argument("--no-prefilter", action="store_true", help="Skip the size/quick/full-hash duplicate check")
    ap.add_argument("--workers", type=int, default=1, help="Parallel processes for slide extraction")
    ap.add_argument("--report-json", help="Write the space/performance report to this JSON file")
    ap.add_argument("--profile", help="Run under cProfile and save the profile here (e.g. run.prof)")
    ap.add_argument("--snakeviz", action="store_true", help="Open the --profile output in snakeviz")
    ap.add_argument("--top", type=int, default=10, help="How many slowest files to list")
    ap.add_argument("--quiet", action="store_true", help="Only print the summary")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.profile:
        return speedtest(lambda: run(args), args.profile, args.snakeviz)
    return run(args)


def run(args) -> int:
    t_start = time.perf_counter()
    stats = Stats()
    stage: dict[str, float] = {}
    log = (lambda *a, **k: None) if args.quiet else print

    settings = {
        "text_only": args.text_only,
        "keep_page_numbers": args.keep_page_numbers,
        "fast_images": args.fast_images,
    }

    # -- load index -------------------------------------------------------
    t = time.perf_counter()
    index_path = Path(args.index)
    with stats.phase("io.index_load"):
        if index_path.exists():
            index = SlideIndex.from_json(json.loads(index_path.read_text("utf-8")), stats)
        else:
            index = SlideIndex(stats)
    if index.settings and index.settings != settings and (index.kept or index.discarded):
        print(f"WARNING  index was built with {index.settings}, this run uses {settings}. "
              "Slide hashes will not match; use a separate --index.", file=sys.stderr)
    index.settings = settings
    previously_kept = set(index.kept)
    stage["load"] = time.perf_counter() - t

    # -- 1. scan ----------------------------------------------------------
    t = time.perf_counter()
    with stats.phase("scan"):
        found = collect_files(args.inputs)
    new_files, already = [], 0
    for f in found:
        name = str(f.resolve())
        if name in index.kept or name in index.discarded:
            already += 1
        else:
            new_files.append(name)
    stage["scan"] = time.perf_counter() - t

    # -- 2. prefilter -------------------------------------------------------
    t = time.perf_counter()
    matcher = ContentMatcher(index.files, stats)
    duplicate_of: dict[str, str] = {}
    to_extract: list[str] = []
    extract_set: set[str] = set()
    sizes: dict[str, int] = {}
    for name in new_files:
        try:
            sizes[name] = os.stat(name).st_size
        except OSError as exc:
            print(f"ERROR  {name}: {exc}", file=sys.stderr)
            continue
        if args.no_prefilter:
            index.files.setdefault(name, {})["size"] = sizes[name]
            to_extract.append(name)
            continue
        original = matcher.register(name, sizes[name])
        # Only trust originals whose slides are known: this run, kept, or discarded
        # (discarded files' slides are all covered by kept files by invariant).
        if original and (original in index.kept or original in index.discarded or original in extract_set
                         or original in duplicate_of):
            duplicate_of[name] = duplicate_of.get(original, original)
        else:
            to_extract.append(name)
            extract_set.add(name)
    stage["prefilter"] = time.perf_counter() - t

    # -- 3. extract ---------------------------------------------------------
    t = time.perf_counter()
    hashed: dict[str, set[str]] = {}
    per_file: list[dict] = []
    errors: dict[str, str] = {}
    jobs = [(n, not args.text_only, not args.keep_page_numbers, args.fast_images) for n in to_extract]
    if args.workers > 1 and len(jobs) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(_extract_worker, jobs, chunksize=1))
    else:
        results = map(_extract_worker, jobs)
    for name, hashes, secs, wstats, err in results:
        stats.merge(wstats)
        rec = {"file": name, "ext": Path(name).suffix.lower(), "bytes": sizes.get(name, 0),
               "seconds": secs, "slides": len(hashes), "distinct": len(set(hashes))}
        if err:
            errors[name] = err
            rec["error"] = err
            print(f"ERROR  {name}: {err}", file=sys.stderr)
        else:
            hashed[name] = set(hashes)
            index.files.setdefault(name, {})["slides"] = len(hashes)
            log(f"hashed {name}  ({len(hashes)} slides, {len(set(hashes))} distinct, {secs*1000:.0f} ms)")
        per_file.append(rec)
    # A copy of a file that failed to parse is unknown too: retry both next run.
    for name, orig in list(duplicate_of.items()):
        if orig in errors:
            errors[name] = f"identical to {orig}, which failed to parse"
            del duplicate_of[name]
    stage["extract"] = time.perf_counter() - t

    # -- 4. decide ----------------------------------------------------------
    t = time.perf_counter()
    if args.mode == "online":
        for name in new_files:
            if name in duplicate_of:
                index.discarded[name] = f"identical file content to {duplicate_of[name]}"
                log(f"DUP    {name}  (= {duplicate_of[name]})")
                continue
            if name not in hashed:
                continue
            kept, evicted = index.add(name, hashed[name])
            log(f"{'KEEP ' if kept else 'SKIP '}  {name}")
            for e in evicted:
                log(f"EVICT  {e}")
    else:
        old_discarded = dict(index.discarded)
        files_meta = index.files
        index = SlideIndex.plan_batch({**index.kept, **hashed}, stats)
        index.files, index.settings = files_meta, settings
        for n, r in old_discarded.items():
            index.discarded.setdefault(n, r)
        for name, orig in duplicate_of.items():
            index.discarded[name] = f"identical file content to {orig}"
    stage["decide"] = time.perf_counter() - t

    # -- 5. act -------------------------------------------------------------
    t = time.perf_counter()
    to_remove = [n for n in index.discarded if n in hashed or n in duplicate_of or n in previously_kept]
    moved = 0
    if args.trash:
        with stats.phase("io.trash"):
            for n in to_remove:
                moved += move_to_trash(n, Path(args.trash))
    with stats.phase("io.index_save"):
        index_path.write_text(json.dumps(index.to_json(), indent=2), encoding="utf-8")
    stage["act"] = time.perf_counter() - t
    total_seconds = time.perf_counter() - t_start

    # -- report ---------------------------------------------------------------
    removed_bytes = sum(_size_of(n, index) for n in to_remove)
    kept_bytes = sum(_size_of(n, index) for n in index.kept)
    considered_bytes = kept_bytes + removed_bytes
    lifetime_removed = sum(_size_of(n, index) for n in index.discarded)
    parsed = [r for r in per_file if "error" not in r]
    parse_secs = sum(r["seconds"] for r in parsed)
    parse_bytes = sum(r["bytes"] for r in parsed)
    parse_slides = sum(r["slides"] for r in parsed)
    avg_parse = parse_secs / len(parsed) if parsed else 0.0

    report = {
        "settings": {**settings, "mode": args.mode, "workers": args.workers,
                     "prefilter": not args.no_prefilter, "trash": args.trash},
        "files": {
            "found": len(found), "already_indexed": already, "new": len(new_files),
            "duplicates_by_content": len(duplicate_of), "parsed": len(parsed), "errors": len(errors),
        },
        "space": {
            "removed_this_run_files": len(to_remove),
            "removed_this_run_bytes": removed_bytes,
            "kept_files": len(index.kept),
            "kept_bytes": kept_bytes,
            "saved_percent": (100 * removed_bytes / considered_bytes) if considered_bytes else 0.0,
            "applied": bool(args.trash),
            "removed_all_runs_files": len(index.discarded),
            "removed_all_runs_bytes": lifetime_removed,
        },
        "slides": {"unique": len(index.coverage), "parsed_this_run": parse_slides},
        "time": {
            "total_seconds": total_seconds,
            "stages_seconds": stage,
            "avg_seconds_per_new_file": total_seconds / len(new_files) if new_files else 0.0,
            "avg_seconds_per_parsed_file": avg_parse,
            "avg_ms_per_slide": 1000 * parse_secs / parse_slides if parse_slides else 0.0,
            "parse_mb_per_second": (parse_bytes / 2**20) / parse_secs if parse_secs else 0.0,
            "est_seconds_saved_by_prefilter": avg_parse * len(duplicate_of),
        },
        "prefilter": dict(matcher.counts),
        "phases": stats.to_dict(),
        "slowest_files": sorted(per_file, key=lambda r: -r["seconds"])[: args.top],
        "hints": _bottleneck_hints(stats, {"workers": args.workers}),
    }

    print_report(report, index, to_remove, args)

    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report saved:  {args.report_json}")
    return 0


def print_report(report: dict, index: SlideIndex, to_remove: list[str], args) -> None:
    sp, tm, fl = report["space"], report["time"], report["files"]
    print()
    print(f"Unique slides: {report['slides']['unique']}")
    print(f"Kept files:    {sp['kept_files']}  ({human_bytes(sp['kept_bytes'])})")
    if not args.quiet:
        for n in sorted(index.kept):
            print(f"  + {n}")
    print(f"Not kept:      {len(to_remove)}{'  (moved to ' + args.trash + ')' if args.trash else '  (report only)'}")
    if not args.quiet:
        for n in to_remove:
            print(f"  - {n}  [{index.discarded[n]}]")
    print(f"Index saved:   {args.index}")

    print("\nSpace")
    verb = "Saved (moved to trash)" if sp["applied"] else "Would save"
    print(f"  {verb:<24}{human_bytes(sp['removed_this_run_bytes']):>12}  "
          f"({sp['saved_percent']:.1f}% of {human_bytes(sp['kept_bytes'] + sp['removed_this_run_bytes'])})")
    print(f"  {'Kept collection':<24}{human_bytes(sp['kept_bytes']):>12}")
    print(f"  {'Not kept, all runs':<24}{human_bytes(sp['removed_all_runs_bytes']):>12}  "
          f"({sp['removed_all_runs_files']} files)")

    print("\nPerformance")
    print(f"  Total run time          {tm['total_seconds']:.3f} s")
    print(f"  Files                   {fl['found']} found, {fl['already_indexed']} already indexed, "
          f"{fl['duplicates_by_content']} identical copies, {fl['parsed']} parsed, {fl['errors']} errors")
    print(f"  Avg per new file        {tm['avg_seconds_per_new_file']*1000:.1f} ms  (whole run / new files)")
    print(f"  Avg per parsed file     {tm['avg_seconds_per_parsed_file']*1000:.1f} ms")
    print(f"  Avg per slide           {tm['avg_ms_per_slide']:.2f} ms")
    print(f"  Parse throughput        {tm['parse_mb_per_second']:.2f} MB/s")
    if fl["duplicates_by_content"]:
        print(f"  Prefilter saved approx  {tm['est_seconds_saved_by_prefilter']:.2f} s of parsing")
    pf = report["prefilter"]
    if pf:
        print(f"  Prefilter funnel        {pf.get('unique_size', 0)} unique size -> "
              f"{pf.get('size_match', 0)} size match -> {pf.get('quick_match', 0)} quick-hash match -> "
              f"{pf.get('duplicates', 0)} identical")

    print("\n  Stage (wall time)       seconds      %")
    for name, secs in tm["stages_seconds"].items():
        pct = 100 * secs / tm["total_seconds"] if tm["total_seconds"] else 0
        print(f"    {name:<20}{secs:>11.3f}  {pct:>5.1f}")

    phases = report["phases"]
    if phases:
        par = "  (summed across workers)" if args.workers > 1 else ""
        print(f"\n  Phase{par}")
        print(f"    {'name':<22}{'seconds':>9}{'calls':>8}{'avg ms':>9}{'MB':>9}")
        for name, v in sorted(phases.items(), key=lambda kv: -kv[1]["seconds"]):
            avg = 1000 * v["seconds"] / v["calls"] if v["calls"] else 0
            mb = f"{v['bytes']/2**20:.1f}" if v["bytes"] else ""
            print(f"    {name:<22}{v['seconds']:>9.3f}{v['calls']:>8}{avg:>9.2f}{mb:>9}")

    if report["slowest_files"]:
        print(f"\n  Slowest files")
        for r in report["slowest_files"]:
            tag = "  ERROR" if "error" in r else ""
            print(f"    {r['seconds']*1000:>9.0f} ms  {r['slides']:>4} slides  "
                  f"{human_bytes(r['bytes']):>9}  {r['file']}{tag}")

    for h in report["hints"]:
        print(f"\n  Hint: {h}")


if __name__ == "__main__":
    sys.exit(main())



"""
slide_dedupe.py

Find the smallest collection of PDF, PowerPoint and image files that
preserves all unique slides, and report how much disk space the cleanup saves
and where the run spends its time.

The script compares individual slides across files. Each slide is identified
by a SHA-256 hash calculated from:
    - Normalized text extracted from the slide.
    - SHA-256 hashes of images embedded in the slide.

Two slides are considered identical if their normalized text and image hashes
match. Page numbers are ignored by default.

Image files (.png, .jpg, .jpeg, .gif, .bmp, .webp, .tif, .tiff) count as a file
with one slide: no text and one image, hashed from the file's bytes just like a
picture inside a PPTX. So two copies of the same image are duplicates, and an
image is also a duplicate of a slide that holds only that same picture.

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
    - Individual PDF, PPTX or image (PNG, JPG, ...) files.
    - Multiple files in one command.
    - One or more folders.
    - Any combination of files and folders.

Folders are scanned recursively. Files are processed in sorted order within
each folder. Supported extensions are defined by SUPPORTED (.pdf, .pptx and
the image types in IMAGE_EXTS).

OPTIONS
-------

--index PATH
    Path to the JSON file used to store the collection's state.
    Default: data/slide_index.json (in the project folder)

--mode {online,batch}
    Select the processing mode. Default: online.

--trash PATH
    Move discarded and evicted files into this folder instead of leaving
    them in their original locations.
    Without this option, files are never moved or deleted, and the space
    report shows what you WOULD save.

--text-only
    Ignore embedded images when comparing slides. Only extracted text is used.
    Image files are still compared by their content (they have no text).

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
    python src/dedupe/slide_dedupe.py data/raw

Find bottlenecks with cProfile + snakeviz:
    python src/dedupe/slide_dedupe.py data/raw --index /tmp/test_index.json --profile run.prof --snakeviz

Faster run on a big folder:
    python src/dedupe/slide_dedupe.py data/raw --workers 4 --fast-images

Track runs over time:
    python src/dedupe/slide_dedupe.py data/raw --report-json reports/run_2026-11-13.json

Move discarded files:
    python src/dedupe/slide_dedupe.py data/raw --mode batch --trash discarded/

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
  produce different hashes. The same goes for image files: a PNG and a JPG
  of the same picture, or a resized copy, are different images.
- PDF text extraction runs on pypdf with output-identical speed-ups (a faster
  content-stream parser plus per-file font and form caches, see "pypdf
  speed-ups" below). They give the same text, slide hashes and errors as plain
  pypdf, and are used only with pypdf 6.x. Set SLIDE_DEDUPE_PYPDF_SPEEDUPS=0 to
  turn them off.
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

# The index (state between runs) lives with the data: <project>/data/slide_index.json
DEFAULT_INDEX = Path(__file__).resolve().parents[2] / "data" / "slide_index.json"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
SUPPORTED = {".pdf", ".pptx"} | IMAGE_EXTS

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


# --------------------------------------------------------------------------
# pypdf speed-ups for text extraction (output-identical)
# --------------------------------------------------------------------------
#
# Profiling page.extract_text() on real slide decks (Keynote, PowerPoint,
# LibreOffice and LaTeX/beamer exports) shows these hotspots:
#
#   1. ContentStream._parse_content_stream, about 60% of extract_text: pypdf
#      tokenizes each content stream one byte at a time (BytesIO.read(1) /
#      seek(-1)). We replace it with a regex tokenizer that builds the same
#      operations list, using the same pypdf object types, constructors and
#      parsing-budget accounting.
#   2. Repeated parsing: form XObjects shared by many pages (slide
#      backgrounds, master elements) are parsed again on every page. When the
#      same stream turns up a second time in a file, its parsed operations are
#      cached for the rest of that file.
#   3. Font.from_font_resource, about 10%: pypdf rebuilds every font, including
#      parsing its /ToUnicode CMap, on every page, although a deck reuses the
#      same few font objects on all pages. We cache it per file.
#   4. is_char_neutral / is_char_rtl, a few %: called for every character
#      shown, with a cache that only lasts one call. Memoized (pure functions).
#
# Safety: the tokenizer handles only the plain, common token forms itself.
# Less common objects (strings with escapes or nested parentheses, hex strings
# containing whitespace, names with '#' or non-ASCII bytes, non-trivial
# dictionaries, booleans) are read by pypdf's own read_object at the same
# position. When something is unusual for the whole stream (inline images,
# comments, very long tokens, deep nesting, a limit being hit, or any
# exception), the fast result is thrown away and pypdf's original parser runs.
# That keeps the result exactly what pypdf would produce, errors such as
# LimitReachedError included. The speed-ups only turn on for pypdf 6.x, the
# version they were checked against.
#
# Set SLIDE_DEDUPE_PYPDF_SPEEDUPS=0 to run plain pypdf, for example to compare results or timings.
PYPDF_SPEEDUPS = os.environ.get("SLIDE_DEDUPE_PYPDF_SPEEDUPS", "1") != "0"

_WS = rb"[\x00\t\n\f\r ]*"              # pypdf WHITESPACES (top level, dictionaries)
_WS_ARRAY = rb"[\x00\t\n\x0b\f\r ]*"    # isspace() or WHITESPACES (inside arrays)
# Plain name: printable ASCII up to the next delimiter (no backtracking possible).
_NAME = rb"/[!-$&-'*-.0-;=?-Z\\^-z|~]*(?=[\s()<>\[\]{}/%]|\Z)"
_NUM = rb"[0-9+\-.][+,\-.0-9]*"   # NumberObject.NumberPattern quirk: '+-.' is a range that includes ','
_TOKENS = (
    rb"(?:(?P<num>" + _NUM + rb")"
    rb"|(?P<name>" + _NAME + rb")"
    rb"|(?P<str>\([^()\\]*\))"
    rb"|(?P<hex><[0-9A-Fa-f]*>)"
    rb"|(?P<dict><<)"
    rb"|(?P<op>[A-Za-z'\"][^\s()<>\[\]{}/%]*)"
    rb"|(?P<open>\[)"
    rb"|(?P<close>\])"
    rb"|(?P<comment>%)"
    rb"|(?P<eof>\Z)"
    rb"|(?P<other>.))"
)
_FAST_TOP = re.compile(_WS + _TOKENS, re.S)
_FAST_ARRAY = re.compile(_WS_ARRAY + _TOKENS, re.S)
# One "/Key value" entry of a plain dictionary such as << /MCID 3 >> (marked content).
_FAST_DICT_ENTRY = re.compile(
    _WS + rb"(?:(?P<key>" + _NAME + rb")" + _WS
    + rb"(?:(?P<num>" + _NUM + rb")|(?P<name>" + _NAME + rb")|(?P<str>\([^()\\]*\))|(?P<hex><[0-9A-Fa-f]*>))"
    + rb"|(?P<end>>>))", re.S)
_FAST_STREAM_KEYWORD = re.compile(_WS + rb"stream")
# The most common operation: plain numbers then an operator, e.g. "1 0 0 1 72 720 cm",
# "10 20 m" or "Q". Each number is followed by whitespace, so the match cannot split
# one number into two; the operator is read up to the next delimiter, as pypdf does.
_FAST_NUMBER_OP = re.compile(
    _WS + rb"((?:" + _NUM + rb"[\t\n\f\r ]+)*)([A-Za-z'\"][^\s()<>\[\]{}/%]*)")
_MAYBE_INDIRECT = re.compile(rb"\sR[^a-zA-Z]")  # cheap pre-check for "12 0 R"
_NUMBER_PEEK = 20        # read_object peeks 20 bytes for numbers / indirect references
_MAX_FAST_OPERATOR = 64  # pypdf limit is 128 (approximate, chunked): stay well below
_MAX_FAST_NAME = 1024    # pypdf limit is 4096
_MAX_FAST_NESTING = 32   # deeper arrays: let pypdf decide (it may hit RecursionError)
_MAX_FAST_DICT = 256     # entries; pypdf limit is far higher


class _FastParseFallback(Exception):
    """Internal: let pypdf's original parser handle this stream."""


_pypdf_speedups: dict | None = None   # originals, once installed
_file_cache: dict | None = None       # per-file caches while _pypdf_file_cache() is active


def _install_pypdf_speedups() -> bool:
    """Patch pypdf once per process. Returns False if disabled or this pypdf is not supported."""
    global _pypdf_speedups
    if _pypdf_speedups is not None:
        return bool(_pypdf_speedups)
    _pypdf_speedups = {}
    if not PYPDF_SPEEDUPS:
        return False
    try:
        import pypdf
        from pypdf._configuration import get_configuration
        from pypdf.generic import (ArrayObject, ByteStringObject, DictionaryObject, FloatObject,
                                   NameObject, NumberObject)
        from pypdf.generic import _data_structures as ds
        from pypdf.generic._font import Font
        from pypdf.generic._utils import StreamParsingBudget, create_string_object
        if int(pypdf.__version__.split(".")[0]) != 6:
            return False
        original_parse = ds.ContentStream._parse_content_stream
        original_font = Font.from_font_resource.__func__
        indirect, read_object = ds.IndirectPattern, ds.read_object
    except Exception:  # unknown pypdf internals: leave pypdf untouched
        return False

    top_match, array_match, number_op = _FAST_TOP.match, _FAST_ARRAY.match, _FAST_NUMBER_OP.match
    entry_match, stream_keyword = _FAST_DICT_ENTRY.match, _FAST_STREAM_KEYWORD.match
    new_float, new_int = float.__new__, int.__new__

    def fast_ops(data: bytes, enc):
        """The operations list pypdf would build for `data`, or raise _FastParseFallback."""
        config = get_configuration()
        budget = StreamParsingBudget(remaining_budget=config.stream_parsing_work_maximum_count)
        max_elements = config.array_maximum_element_count
        bytes_mode = enc == "bytes"
        # "12 0 R" in a content stream makes pypdf fail (no reader there); check per number only if present.
        check_indirect = _MAYBE_INDIRECT.search(data) is not None and indirect.search(data) is not None
        bio = None

        def number(m, group="num"):
            s = m.group(group)
            if len(s) >= _NUMBER_PEEK - 1 or (
                    check_indirect and indirect.match(data, m.start(group), m.start(group) + _NUMBER_PEEK)):
                raise _FastParseFallback
            if b"." in s:
                try:
                    return new_float(FloatObject, float(s))
                except ValueError:
                    return FloatObject(s)   # pypdf logs a warning and uses 0.0
            try:
                return new_int(NumberObject, int(s))
            except ValueError:
                return NumberObject(s)      # pypdf logs a warning and uses 0

        def string(m, group):
            s = data[m.start(group) + 1:m.end(group) - 1]
            if group == "hex":
                s = bytes.fromhex((s + b"0" if len(s) % 2 else s).decode())
            return ByteStringObject(s) if bytes_mode else create_string_object(s, enc)

        def name(m, group="name"):
            s = m.group(group)
            if len(s) > _MAX_FAST_NAME or b"#" in s:
                return None
            return NameObject(s.decode())

        def delegate(start):
            nonlocal bio
            if bio is None:
                from io import BytesIO
                bio = BytesIO(data)
            bio.seek(start)
            obj = read_object(bio, None, enc, parsing_budget=budget)
            return obj, bio.tell()

        def read_dict(start):
            items, pos = {}, start + 2
            while True:
                m = entry_match(data, pos)
                if m is None or len(items) >= _MAX_FAST_DICT:
                    return delegate(start)     # not a plain dictionary
                pos = m.end()
                if m.lastgroup == "end":
                    break
                key = name(m, "key")
                kind = m.lastgroup
                value = (number(m, kind) if kind == "num" else name(m, kind) if kind == "name"
                         else string(m, kind))
                if key is None or value is None or key in items:
                    return delegate(start)
                items[key] = value
                budget.remaining_budget -= 2
            if stream_keyword(data, pos):
                raise _FastParseFallback       # "<< >> stream": leave to pypdf
            d = DictionaryObject()
            d.update(items)
            return d, pos

        def read_array(pos, depth):
            if depth > _MAX_FAST_NESTING:
                raise _FastParseFallback
            arr, remaining = ArrayObject(), max_elements
            while True:
                m = array_match(data, pos)
                kind = m.lastgroup
                if kind == "close" or kind == "eof":
                    return arr, m.end()
                remaining -= 1
                budget.remaining_budget -= 1
                if remaining < 0 or kind == "comment" or data[m.start(kind):m.start(kind) + 1] == b"n":
                    raise _FastParseFallback   # limit / comment / null shortcut: rare, leave to pypdf
                pos = m.end()
                if kind == "str" or kind == "hex":
                    arr.append(string(m, kind))
                elif kind == "num":
                    arr.append(number(m))
                elif kind == "open":
                    sub, pos = read_array(pos, depth + 1)
                    arr.append(sub)
                elif kind == "name" and (obj := name(m)) is not None:
                    arr.append(obj)
                elif kind == "dict":
                    obj, pos = read_dict(m.start(kind))
                    arr.append(obj)
                else:                          # booleans, complex strings/names, ...
                    obj, pos = delegate(m.start(kind))
                    arr.append(obj)

        def number_run(parts):
            if max(map(len, parts)) >= _NUMBER_PEEK - 1:
                raise _FastParseFallback
            try:
                return [new_float(FloatObject, float(s)) if b"." in s else new_int(NumberObject, int(s))
                        for s in parts]
            except ValueError:  # e.g. "1.2.3" or "-": pypdf logs a warning and uses 0
                return [FloatObject(s) if b"." in s else NumberObject(s) for s in parts]

        ops, operands, pos = [], [], 0
        while True:
            if not check_indirect and (m := number_op(data, pos)) is not None:
                nums, op = m.groups()
                if op == b"BI" or len(op) > _MAX_FAST_OPERATOR:
                    raise _FastParseFallback   # inline image / suspicious operator
                if nums:
                    parts = nums.split()
                    budget.remaining_budget -= len(parts)
                    operands.extend(number_run(parts))
                budget.remaining_budget -= 1
                ops.append((operands, op))
                operands = []
                pos = m.end()
                continue
            m = top_match(data, pos)
            kind = m.lastgroup
            if kind == "eof":
                break
            pos = m.end()
            budget.remaining_budget -= 1
            if kind == "op":
                op = m.group(kind)
                if op == b"BI" or len(op) > _MAX_FAST_OPERATOR:
                    raise _FastParseFallback   # inline image / suspicious operator
                ops.append((operands, op))
                operands = []
            elif kind == "num":
                operands.append(number(m))
            elif kind == "name" and (obj := name(m)) is not None:
                operands.append(obj)
            elif kind == "open":
                obj, pos = read_array(pos, 1)
                operands.append(obj)
            elif kind == "str" or kind == "hex":
                operands.append(string(m, kind))
            elif kind == "dict":
                obj, pos = read_dict(m.start(kind))
                operands.append(obj)
            elif kind == "comment" or kind == "close":
                raise _FastParseFallback
            else:                              # complex strings/names, ...: pypdf's own reader
                obj, pos = delegate(m.start(kind))
                operands.append(obj)
        if budget.remaining_budget < 0:
            raise _FastParseFallback           # pypdf raises LimitReachedError here
        return ops

    def fast_parse(self, stream):
        try:
            data = stream.getvalue()
        except AttributeError:
            return original_parse(self, stream)
        cache = _file_cache
        key = (data, self.forced_encoding if isinstance(self.forced_encoding, (str, type(None))) else id(self))
        if cache is not None and key in cache["ops"]:
            self._operations = [(list(operands), op) for operands, op in cache["ops"][key]]
            return None
        try:
            ops = fast_ops(data, self.forced_encoding)
        except Exception:      # _FastParseFallback, a limit, a malformed stream, ...
            self._operations = []
            return original_parse(self, stream)
        self._operations = ops
        if cache is not None:
            # Cache streams seen twice in this file (shared form XObjects), not every page.
            seen = (hashlib.sha1(data).digest(), key[1])   # don't keep every page's bytes alive
            if seen in cache["seen"]:
                cache["ops"][key] = [(list(operands), op) for operands, op in ops]
            else:
                cache["seen"].add(seen)
        return None

    def cached_font(cls, pdf_font_dict):
        cache = _file_cache
        if cache is None:
            return original_font(cls, pdf_font_dict)
        hit = cache["fonts"].get(id(pdf_font_dict))
        if hit is not None and hit[0] is pdf_font_dict:
            return hit[1]
        font = original_font(cls, pdf_font_dict)
        cache["fonts"][id(pdf_font_dict)] = (pdf_font_dict, font)  # holds the dict: ids stay unique
        return font

    ds.ContentStream._parse_content_stream = fast_parse
    Font.from_font_resource = classmethod(cached_font)
    # get_display_str classifies every character it shows, and its own caches only
    # last one call. Both checks are pure functions of their arguments.
    import functools
    from pypdf import _text_extraction as te
    te.is_char_neutral = functools.lru_cache(maxsize=65536)(te.is_char_neutral)
    te.is_char_rtl = functools.lru_cache(maxsize=65536)(te.is_char_rtl)
    _pypdf_speedups.update(parse=original_parse, font=original_font, fast_ops=fast_ops)
    return True


@contextmanager
def _pypdf_file_cache():
    """Per-file caches: fonts shared by the pages, streams parsed more than once."""
    global _file_cache
    previous, _file_cache = _file_cache, {"fonts": {}, "ops": {}, "seen": set()}
    try:
        yield
    finally:
        _file_cache = previous


def pdf_slides(path: Path, use_images: bool = True, stats: Stats | None = None,
               fast_images: bool = False):
    """Yield (text, [image_hash, ...]) for each page of a PDF."""
    stats = stats or Stats()
    with stats.phase("pdf.open"):
        from pypdf import PdfReader
        _install_pypdf_speedups()
        reader = PdfReader(str(path))
        pages = reader.pages
    with _pypdf_file_cache():
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


def image_slides(path: Path, stats: Stats | None = None):
    """Yield one (text, [image_hash]) for an image file: no text, the file's bytes.

    Hashed like a picture inside a PPTX (shape.image.blob is the original file
    bytes), so a slide that holds only this picture gets the same slide hash.
    """
    stats = stats or Stats()
    with stats.phase("image.read"):
        data = path.read_bytes()
    stats.bytes["image.read"] += len(data)
    yield "", [sha256(data)]


def file_slide_hashes(path: Path, use_images: bool = True, drop_page_numbers: bool = True,
                      stats: Stats | None = None, fast_images: bool = False) -> list[str]:
    """Return the slide hashes of a file, in slide order (duplicates kept)."""
    stats = stats or Stats()
    ext = path.suffix.lower()
    if ext == ".pdf":
        slides = pdf_slides(path, use_images, stats, fast_images)
    elif ext == ".pptx":
        slides = pptx_slides(path, use_images, stats)
    elif ext in IMAGE_EXTS:
        # Always by content, even with --text-only: an image has no text, so
        # ignoring its pixels would make every image file look the same.
        slides = image_slides(path, stats)
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

def _is_lock_file(f: Path) -> bool:
    """Office lock files ("~$deck.pptx") exist while a file is open; they are not decks."""
    return f.name.startswith("~$")


def collect_files(inputs: list[str]) -> list[Path]:
    """Expand folders (recursively, sorted) and keep the given order of files."""
    files, seen = [], set()
    for item in inputs:
        p = Path(item)
        found = sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED) if p.is_dir() else [p]
        for f in found:
            key = str(f.resolve())
            if key not in seen and f.suffix.lower() in SUPPORTED and not _is_lock_file(f):
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
    ap = argparse.ArgumentParser(description="Keep the fewest PDF/PPTX/image files that still hold every unique slide.")
    ap.add_argument("inputs", nargs="+", help="PDF/PPTX/image files and/or folders")
    ap.add_argument("--index", default=str(DEFAULT_INDEX),
                    help="State file (default: data/slide_index.json)")
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
    report, index, to_remove = analyze(args)
    print_report(report, index, to_remove, args)
    if args.report_json:
        Path(args.report_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report saved:  {args.report_json}")
    return 0


def analyze(args) -> tuple[dict, "SlideIndex", list[str]]:
    """Run the whole pipeline (scan -> act) and return (report, index, files not kept).

    `args` holds the CLI options (see build_parser); the website calls this directly.
    The report also lists every file not kept ("removed") and every kept file ("kept").
    """
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
        "removed": [{"file": n, "reason": index.discarded[n], "bytes": _size_of(n, index)}
                    for n in to_remove],
        "kept": [{"file": n, "bytes": _size_of(n, index)} for n in sorted(index.kept)],
        "moved_files": moved,
    }
    return report, index, to_remove


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

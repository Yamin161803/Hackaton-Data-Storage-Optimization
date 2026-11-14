

"""
check_pypdf_speedups.py

Checks that the pypdf speed-ups in slide_dedupe.py give exactly the same
text as plain pypdf on your own PDFs, and times both.

    python src/dedupe/check_pypdf_speedups.py data/raw [more files/folders]

For every page it compares page.extract_text() (and any error raised) between
plain pypdf and the patched version. Exit code 1 if anything differs.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slide_dedupe as sd  # noqa: E402


def extract(path: Path, fast: bool):
    from pypdf import PdfReader
    t0 = time.perf_counter()
    pages = []
    try:
        reader = PdfReader(str(path))
        if fast:
            with sd._pypdf_file_cache():
                for page in reader.pages:
                    pages.append(page.extract_text() or "")
        else:
            for page in reader.pages:
                pages.append(page.extract_text() or "")
        result = ("ok", pages)
    except Exception as exc:  # compare errors too
        result = ("error", (pages, type(exc).__name__, str(exc)))
    return result, time.perf_counter() - t0


def main(argv: list[str]) -> int:
    logging.disable(logging.WARNING)   # pypdf warnings would only add noise here
    files = [f for f in sd.collect_files(argv) if f.suffix.lower() == ".pdf"]
    if not files:
        print("No PDF files found.")
        return 2
    plain = {f: extract(f, fast=False) for f in files}       # before patching pypdf
    if not sd._install_pypdf_speedups():
        print("Speed-ups not active (disabled, or not pypdf 6.x): nothing to compare.")
        return 2
    fast = {f: extract(f, fast=True) for f in files}
    bad, t_plain, t_fast = 0, 0.0, 0.0
    for f in files:
        (a, ta), (b, tb) = plain[f], fast[f]
        t_plain, t_fast = t_plain + ta, t_fast + tb
        same = a == b
        bad += not same
        print(f"{'OK  ' if same else 'DIFF'}  {ta*1000:8.0f} ms -> {tb*1000:8.0f} ms  {f}")
    print(f"\n{len(files)} PDFs, {bad} with differences. "
          f"Text extraction {t_plain:.2f} s -> {t_fast:.2f} s ({t_plain / max(t_fast, 1e-9):.2f}x faster)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["."]))


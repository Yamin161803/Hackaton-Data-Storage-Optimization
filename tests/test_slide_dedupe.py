

"""Tests for src/dedupe/slide_dedupe.py. Run with: python -m pytest tests/test_slide_dedupe.py"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dedupe import slide_dedupe as slide_dedupe_v2  # noqa: E402
from dedupe.slide_dedupe import (ContentMatcher, SlideIndex, file_slide_hashes,  # noqa: E402
                                    main, normalize_text, QUICK_CHUNK)


def _all(index):
    return set(index.coverage)


def test_user_scenario_online():
    idx = SlideIndex()
    A = {"s1", "s2", "s3"}
    B = A | {"s4"}                 # A + one extra slide
    C = {"s2", "s3", "s4", "s5"}   # most of B + new slide
    D = {"s1", "s5"}               # mix of A and C

    assert idx.add("A", A) == (True, [])
    assert idx.add("B", B) == (True, ["A"])   # B replaces A
    assert idx.add("C", C) == (True, [])      # both B and C kept
    assert idx.add("D", D) == (False, [])     # nothing new
    assert set(idx.kept) == {"B", "C"}
    assert _all(idx) == A | B | C | D         # no unique slide lost


def test_identical_file_is_skipped():
    idx = SlideIndex()
    idx.add("X", {"a", "b"})
    assert idx.add("X copy", {"b", "a"}) == (False, [])


def test_new_file_can_evict_several_old_files():
    idx = SlideIndex()
    idx.add("P", {"a", "b"})
    idx.add("Q", {"c", "d"})
    kept, evicted = idx.add("R", {"a", "b", "c", "d", "e"})
    assert kept and sorted(evicted) == ["P", "Q"]
    assert set(idx.kept) == {"R"}


def test_never_evicts_file_with_unique_slide():
    idx = SlideIndex()
    idx.add("P", {"a", "b"})
    idx.add("Q", {"b", "c"})
    idx.add("R", {"a", "c", "d"})   # P and Q each still hold... nothing unique? a,b: b also in Q
    # P = {a,b}: a in R, b in Q -> redundant, evicted. Q = {b,c}: b now only in Q -> kept.
    assert set(idx.kept) == {"Q", "R"}
    assert _all(idx) == {"a", "b", "c", "d"}


def test_batch_keeps_fewer_files_than_bad_order():
    files = {"small1": {"a", "b"}, "small2": {"c", "d"}, "big": {"a", "b", "c", "d"}}
    idx = SlideIndex.plan_batch(files)
    assert set(idx.kept) == {"big"}


def test_page_numbers_ignored():
    assert normalize_text("Intro\n  3 \n") == normalize_text("Intro\nPage 7")


def _make_pdf(path, slides):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for i, text in enumerate(slides, 1):
        c.drawString(100, 700, text)
        c.drawString(500, 50, str(i))          # page number footer
        c.showPage()
    c.save()


def _make_pptx(path, slides):
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    for text in slides:
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = text
    prs.save(str(path))


def test_end_to_end_files():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_pdf(d / "a.pdf", ["one", "two", "three"])
        _make_pdf(d / "b.pdf", ["one", "two", "three", "four"])
        _make_pdf(d / "c.pdf", ["two", "three", "four", "five"])
        _make_pdf(d / "d.pdf", ["five", "one"])            # reordered -> page numbers differ
        _make_pptx(d / "e.pptx", ["six", "one"])
        assert len(set(file_slide_hashes(d / "b.pdf"))) == 4

        order = [str(d / n) for n in ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pptx"]]
        index_file = d / "index.json"
        main(order + ["--index", str(index_file), "--trash", str(d / "trash")])

        data = json.loads(index_file.read_text())
        kept = {Path(n).name for n in data["kept"]}
        # e.pptx brings "six" and also "one" (the only slide b.pdf had that c.pdf lacks),
        # so b.pdf becomes redundant and is evicted.
        assert kept == {"c.pdf", "e.pptx"}
        assert data["unique_slides"] == 6
        assert sorted(p.name for p in (d / "trash").iterdir()) == ["a.pdf", "b.pdf", "d.pdf"]


# --------------------------------------------------------------------------
# v2: prefilter, space report, workers, fast images
# --------------------------------------------------------------------------

def _run(d, files, *extra):
    report = d / "report.json"
    main([str(d / f) for f in files] + ["--index", str(d / "index.json"), "--report-json", str(report),
                                        "--quiet", *extra])
    return json.loads(report.read_text()), json.loads((d / "index.json").read_text())


def test_prefilter_quick_hash_match_but_full_hash_differs():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        size = 10 * QUICK_CHUNK
        a = bytearray(b"x" * size)
        b = bytearray(a)
        b[QUICK_CHUNK + 5] = ord("y")          # outside the three 64 KB samples
        c = bytearray(b"z" * size)             # same size, different samples
        for n, data in {"a": a, "b": b, "c": c, "a2": a}.items():
            (d / n).write_bytes(data)
        m = ContentMatcher()
        assert m.register(str(d / "a")) is None
        assert m.register(str(d / "b")) is None   # quick hash matches, full hash does not
        assert m.register(str(d / "c")) is None   # quick hash differs
        assert m.register(str(d / "a2")) == str(d / "a")
        assert m.counts["quick_match"] == 2 and m.counts["duplicates"] == 1


def test_prefilter_skips_full_read_for_unique_sizes():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "a").write_bytes(b"1" * 100)
        (d / "b").write_bytes(b"1" * 200)
        m = ContentMatcher()
        m.register(str(d / "a")); m.register(str(d / "b"))
        assert m.counts["unique_size"] == 2
        assert "quick" not in m.records[str(d / "a")] and "full" not in m.records[str(d / "a")]


def test_identical_copy_is_not_parsed_and_space_is_counted(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_pdf(d / "a.pdf", ["one", "two"])
        (d / "copy.pdf").write_bytes((d / "a.pdf").read_bytes())
        _make_pdf(d / "b.pdf", ["one", "two", "three"])
        report, index = _run(d, ["a.pdf", "copy.pdf", "b.pdf"])
        assert report["files"]["duplicates_by_content"] == 1
        assert report["files"]["parsed"] == 2                      # copy.pdf never opened
        assert "identical file content" in index["discarded"][str((d / "copy.pdf").resolve())]
        assert {Path(n).name for n in index["kept"]} == {"b.pdf"}
        removed = (d / "a.pdf").stat().st_size + (d / "copy.pdf").stat().st_size
        assert report["space"]["removed_this_run_bytes"] == removed
        assert report["space"]["kept_bytes"] == (d / "b.pdf").stat().st_size
        assert report["space"]["applied"] is False
        assert (d / "a.pdf").exists()                               # dry run moves nothing


def test_copy_of_file_from_earlier_run_is_caught():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_pdf(d / "a.pdf", ["one", "two"])
        _run(d, ["a.pdf"])
        (d / "later.pdf").write_bytes((d / "a.pdf").read_bytes())
        report, index = _run(d, ["later.pdf"])
        assert report["files"]["duplicates_by_content"] == 1 and report["files"]["parsed"] == 0


def test_workers_give_same_result_as_sequential():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        names = []
        for i, slides in enumerate([["a", "b"], ["a", "b", "c"], ["c", "d"], ["a", "d"]]):
            _make_pdf(d / f"f{i}.pdf", slides); names.append(f"f{i}.pdf")
        _, seq = _run(d, names)
        (d / "index.json").unlink()
        _, par = _run(d, names, "--workers", "2")
        assert seq["kept"] == par["kept"] and seq["discarded"] == par["discarded"]


def _make_pdf_with_image(path, color):
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    c.drawString(100, 700, "same text")
    c.drawImage(ImageReader(Image.new("RGB", (40, 40), color)), 100, 400)
    c.showPage(); c.save()


def test_fast_images_still_tells_images_apart():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_pdf_with_image(d / "red.pdf", "red")
        _make_pdf_with_image(d / "blue.pdf", "blue")
        for fast in (False, True):
            r = file_slide_hashes(d / "red.pdf", fast_images=fast)
            b = file_slide_hashes(d / "blue.pdf", fast_images=fast)
            assert r != b
        assert file_slide_hashes(d / "red.pdf", use_images=False) == file_slide_hashes(d / "blue.pdf", use_images=False)


def test_phase_timings_are_reported():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        _make_pdf(d / "a.pdf", ["one", "two"])
        report, _ = _run(d, ["a.pdf"])
        assert report["phases"]["pdf.text"]["calls"] == 2
        assert report["phases"]["slide.hash"]["calls"] == 2
        assert report["time"]["total_seconds"] > 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


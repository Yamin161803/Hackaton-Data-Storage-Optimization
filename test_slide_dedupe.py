"""Run with: python -m pytest test_slide_dedupe.py  (or python test_slide_dedupe.py)"""
import json
import tempfile
from pathlib import Path

from slide_dedupe import SlideIndex, file_slide_hashes, main, normalize_text


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


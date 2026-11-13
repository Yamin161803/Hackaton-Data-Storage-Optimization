"""Tests for src/notes/pdf_to_markdown.py (PDF -> Markdown with hash-named images)."""
import hashlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from notes import pdf_to_markdown as p2m  # noqa: E402

canvas = pytest.importorskip("reportlab.pdfgen.canvas")
ImageReader = pytest.importorskip("reportlab.lib.utils").ImageReader
Image = pytest.importorskip("PIL.Image")


def png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    return buf.getvalue()


def make_pdf(path: Path, pages) -> Path:
    """pages: [(text, [png bytes, ...]), ...]"""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path))
    for text, images in pages:
        if text:
            c.drawString(72, 750, text)
        for i, data in enumerate(images):
            c.drawImage(ImageReader(io.BytesIO(data)), 72 + 60 * i, 600, 40, 30)
        c.showPage()
    c.save()
    return path


def saved_images(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# ---------- small helpers ----------

def test_hash_image_is_sha256():
    assert p2m.hash_image(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_existing_hashes_uses_file_stem(tmp_path):
    (tmp_path / "abc123.png").write_bytes(b"x")
    (tmp_path / "def456.jpg").write_bytes(b"y")
    assert p2m.existing_hashes(tmp_path) == {"abc123": "abc123.png", "def456": "def456.jpg"}


def test_load_pdf_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        p2m.load_pdf(tmp_path / "nope.pdf")


def test_extract_text_per_page(tmp_path):
    pdf = make_pdf(tmp_path / "t.pdf", [("First page", []), ("", []), ("Third page", [])])
    assert p2m.extract_text(p2m.load_pdf(pdf)) == ["First page", "", "Third page"]


# ---------- images: stored once, named by hash ----------

def test_same_image_on_two_pages_is_saved_once(tmp_path):
    red, blue = png("red"), png("blue")
    pdf = make_pdf(tmp_path / "deck.pdf", [("one", [red]), ("two", [red, blue]), ("three", [])])
    per_page, saved, skipped = p2m.extract_images(p2m.load_pdf(pdf), tmp_path / "images")

    assert (saved, skipped) == (2, 1)
    files = saved_images(tmp_path / "images")
    assert len(files) == 2
    for name in files:                                     # file name = hash of its bytes
        data = (tmp_path / "images" / name).read_bytes()
        assert Path(name).stem == hashlib.sha256(data).hexdigest()
    assert per_page[1][0] in per_page[2]                   # page 2 reuses page 1's image
    assert len(per_page[2]) == 2 and per_page[3] == []


def test_images_are_shared_across_pdfs_in_one_folder(tmp_path):
    shared = png("green")
    a = make_pdf(tmp_path / "a.pdf", [("a", [shared])])
    b = make_pdf(tmp_path / "b.pdf", [("b", [shared, png("yellow")])])
    images = tmp_path / "images"
    assert p2m.extract_images(p2m.load_pdf(a), images)[1:] == (1, 0)
    assert p2m.extract_images(p2m.load_pdf(b), images)[1:] == (1, 1)   # green already there
    assert len(saved_images(images)) == 2


# ---------- markdown ----------

def test_save_markdown_layout(tmp_path):
    md = tmp_path / "x.md"
    p2m.save_markdown(md, "Deck", ["Hello", ""], {1: ["h1.png"], 2: ["h1.png", "h2.png"]},
                      "images")
    assert md.read_text(encoding="utf-8").splitlines() == [
        "# Deck", "",
        "## Page 1", "", "Hello", "", "![h1.png](images/h1.png)", "",
        "## Page 2", "", "![h1.png](images/h1.png)", "", "![h2.png](images/h2.png)"]


def test_convert_end_to_end(tmp_path):
    pdf = make_pdf(tmp_path / "in" / "Talk.pdf", [("Intro slide", [png("red")]),
                                                  ("Results", [png("red")])])
    md = p2m.convert(pdf, tmp_path / "out")

    assert md == tmp_path / "out" / "Talk.md"
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Talk\n") and "Intro slide" in text and "Results" in text
    links = [line for line in text.splitlines() if line.startswith("![")]
    assert len(links) == 2 and links[0] == links[1]        # same image, linked twice
    assert len(saved_images(tmp_path / "out" / "images")) == 1
    for link in links:                                      # every link points at a file
        target = link[link.index("(") + 1:-1]
        assert (md.parent / target).is_file()


def test_convert_default_output_is_folder_named_after_pdf(tmp_path):
    pdf = make_pdf(tmp_path / "Report.pdf", [("x", [])])
    assert p2m.convert(pdf) == tmp_path / "Report" / "Report.md"


def test_rerun_does_not_duplicate_images(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", [("a", [png("red"), png("blue")])])
    p2m.convert(pdf, tmp_path / "out")
    p2m.convert(pdf, tmp_path / "out")
    assert len(saved_images(tmp_path / "out" / "images")) == 2


# ---------- command line ----------

def test_main(tmp_path, capsys):
    pdf = make_pdf(tmp_path / "a.pdf", [("hi", [png("red")])])
    assert p2m.main([str(pdf), "--out", str(tmp_path / "o")]) == 0
    assert "Markdown:" in capsys.readouterr().out
    assert (tmp_path / "o" / "a.md").is_file()


def test_main_missing_pdf(tmp_path, capsys):
    assert p2m.main([str(tmp_path / "missing.pdf")]) == 1
    assert "error:" in capsys.readouterr().err


def test_runs_as_a_script_from_the_project_folder(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", [("hi", [])])
    out = subprocess.run([sys.executable, "src/notes/pdf_to_markdown.py", str(pdf),
                          "--out", str(tmp_path / "o")], cwd=ROOT, capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "o" / "a.md").is_file()

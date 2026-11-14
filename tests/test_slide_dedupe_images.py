"""Image files (PNG, JPG, ...) in slide_dedupe.py: one slide per image, hashed by content."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dedupe import slide_dedupe  # noqa: E402
from dedupe.slide_dedupe import (IMAGE_EXTS, SUPPORTED, file_slide_hashes,  # noqa: E402
                                    sha256, slide_hash)

Image = pytest.importorskip("PIL.Image")


def make_image(path: Path, color, fmt=None, size=(64, 48)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, fmt)
    return path


def run(tmp_path, *inputs, extra=()):
    """Run slide_dedupe on inputs; return (kept names, discarded names)."""
    index = tmp_path / "index.json"
    assert slide_dedupe.main([*map(str, inputs), "--index", str(index), "--quiet", *extra]) == 0
    data = json.loads(index.read_text("utf-8"))
    names = lambda d: sorted(Path(p).name for p in d)  # noqa: E731
    return names(data["kept"]), names(data["discarded"])


def test_image_types_are_supported():
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"):
        assert ext in IMAGE_EXTS and ext in SUPPORTED
    assert {".pdf", ".pptx"} <= SUPPORTED


def test_image_is_one_slide_hashed_by_its_bytes(tmp_path):
    img = make_image(tmp_path / "a.png", "red")
    assert file_slide_hashes(img) == [slide_hash("", [sha256(img.read_bytes())])]


def test_copy_of_an_image_is_not_kept(tmp_path):
    folder = tmp_path / "raw"
    make_image(folder / "Brief.png", "red")
    shutil.copy(folder / "Brief.png", folder / "Copy of Brief.png")
    kept, dropped = run(tmp_path, folder)
    assert len(kept) == 1 and len(dropped) == 1      # one of the two copies goes


def test_different_images_are_all_kept(tmp_path):
    folder = tmp_path / "raw"
    make_image(folder / "a.png", "red")
    make_image(folder / "b.png", "blue")
    make_image(folder / "c.jpg", "green")
    kept, dropped = run(tmp_path, folder)
    assert kept == ["a.png", "b.png", "c.jpg"] and dropped == []


def test_uppercase_extension_and_jpeg(tmp_path):
    folder = tmp_path / "raw"
    make_image(folder / "PHOTO.JPG", "red", "JPEG")
    shutil.copy(folder / "PHOTO.JPG", folder / "photo copy.jpeg")
    kept, dropped = run(tmp_path, folder)
    assert len(kept) == 1 and len(dropped) == 1


def test_text_only_still_compares_images_by_content(tmp_path):
    folder = tmp_path / "raw"
    make_image(folder / "a.png", "red")
    make_image(folder / "b.png", "blue")
    kept, dropped = run(tmp_path, folder, extra=["--text-only"])
    assert kept == ["a.png", "b.png"] and dropped == []   # not "two empty slides"


def test_same_pixels_different_file_format_are_different(tmp_path):
    # documented limit: comparison is by encoded bytes, not visual similarity
    folder = tmp_path / "raw"
    make_image(folder / "a.png", "red")
    make_image(folder / "a.bmp", "red")
    kept, _ = run(tmp_path, folder)
    assert kept == ["a.bmp", "a.png"]


def test_image_already_inside_a_pptx_slide_is_a_duplicate(tmp_path):
    pptx = pytest.importorskip("pptx")
    from pptx.util import Inches
    img = make_image(tmp_path / "pics" / "logo.png", "red")
    deck = pptx.Presentation()
    deck.slides.add_slide(deck.slide_layouts[6]).shapes.add_picture(str(img), Inches(1), Inches(1))
    title = deck.slides.add_slide(deck.slide_layouts[5])
    title.shapes.title.text = "Another slide"
    deck_path = tmp_path / "decks" / "deck.pptx"
    deck_path.parent.mkdir()
    deck.save(deck_path)

    kept, dropped = run(tmp_path, deck_path, img)       # deck first, then the image
    assert kept == ["deck.pptx"] and dropped == ["logo.png"]


def test_images_and_slide_files_mixed_in_one_folder(tmp_path):
    folder = tmp_path / "raw"
    make_image(folder / "x.png", "red")
    (folder / "notes.txt").write_text("not a slide file", encoding="utf-8")
    kept, dropped = run(tmp_path, folder)
    assert kept == ["x.png"] and dropped == []           # .txt is ignored


def test_office_lock_files_are_skipped(tmp_path):
    folder = tmp_path / "raw"
    make_image(folder / "a.png", "red")
    (folder / "~$deck.pptx").write_bytes(b"\x08lock file owner name")   # PowerPoint lock
    assert [f.name for f in slide_dedupe.collect_files([str(folder)])] == ["a.png"]


def test_default_index_is_in_data_folder():
    root = Path(__file__).resolve().parents[1]
    assert slide_dedupe.DEFAULT_INDEX == root / "data" / "slide_index.json"
    assert slide_dedupe.build_parser().parse_args(["x"]).index == str(slide_dedupe.DEFAULT_INDEX)


def test_slide_dedupe_runs_as_a_script_from_the_project_folder(tmp_path):
    import subprocess
    root = Path(__file__).resolve().parents[1]
    make_image(tmp_path / "raw" / "a.png", "red")
    out = subprocess.run([sys.executable, "src/dedupe/slide_dedupe.py", str(tmp_path / "raw"),
                          "--index", str(tmp_path / "i.json"), "--quiet"],
                         cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (tmp_path / "i.json").is_file()

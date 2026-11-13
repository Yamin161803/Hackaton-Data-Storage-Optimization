"""Tests for the fingerprint (videohash) and the two-way lookup (lookup.py)."""
import hashlib
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import lookup  # noqa: E402
import videohash  # noqa: E402
from videohash import QUICK_HASH_BYTES, fingerprint, full_hash, quick_hash  # noqa: E402

MiB = 1024 * 1024


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def notes_for(path: Path, md: Path, **override) -> Path:
    """A notes file whose front matter is the fingerprint of `path`."""
    fp = fingerprint(path)
    front = {"file_size": fp.file_size, "quick_hash": fp.quick_hash,
             "quick_hash_method": fp.quick_hash_method, "full_hash": fp.full_hash,
             "full_hash_algorithm": fp.full_hash_algorithm, "video": f'"{path.name}"',
             **override}
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in front.items()) +
                  "---\n\n# notes\n", encoding="utf-8")
    return md


# ---------- videohash: quick and full hash ----------

def test_full_hash_is_sha256_of_the_file(tmp_path):
    f = write(tmp_path / "a.mp4", b"x" * (3 * MiB + 5))
    assert full_hash(f) == hashlib.sha256(f.read_bytes()).hexdigest()


def test_quick_hash_is_size_plus_first_and_last_mib(tmp_path):
    data = bytes(range(256)) * (3 * MiB // 256) + b"tail"
    f = write(tmp_path / "a.mp4", data)
    expected = hashlib.sha256(len(data).to_bytes(8, "big") + data[:QUICK_HASH_BYTES]
                              + data[-QUICK_HASH_BYTES:]).hexdigest()
    assert quick_hash(f) == expected


def test_quick_hash_small_file_reads_it_once(tmp_path):
    f = write(tmp_path / "a.mp4", b"small")
    expected = hashlib.sha256((5).to_bytes(8, "big") + b"small").hexdigest()
    assert quick_hash(f) == expected


def test_quick_hash_sees_start_end_and_size_but_not_the_middle(tmp_path):
    base = bytearray(b"\0" * (4 * MiB))
    a = write(tmp_path / "a", bytes(base))
    start, end, middle = bytearray(base), bytearray(base), bytearray(base)
    start[10], end[-10], middle[2 * MiB] = 1, 1, 1
    assert quick_hash(write(tmp_path / "s", bytes(start))) != quick_hash(a)
    assert quick_hash(write(tmp_path / "e", bytes(end))) != quick_hash(a)
    assert quick_hash(write(tmp_path / "l", bytes(base) + b"\0")) != quick_hash(a)
    # the middle is not read: that's why the full hash confirms a match
    m = write(tmp_path / "m", bytes(middle))
    assert quick_hash(m) == quick_hash(a) and full_hash(m) != full_hash(a)


def test_fingerprint(tmp_path):
    f = write(tmp_path / "a.mp4", b"abc")
    fp = fingerprint(f)
    assert fp.file_size == 3
    assert fp.full_hash == hashlib.sha256(b"abc").hexdigest()
    assert fp.quick_hash_method == "sha256-size-first-last-1MiB"
    assert fp.full_hash_algorithm == "sha256"


# ---------- video -> notes ----------

def test_notes_for_video(tmp_path):
    video = write(tmp_path / "raw" / "talk.mp4", b"v" * 100)
    other = write(tmp_path / "raw" / "other.mp4", b"o" * 100)
    notes = tmp_path / "notes"
    mine = notes_for(video, notes / "talk.md")
    notes_for(other, notes / "other.md")
    (notes / "plain.md").write_text("# no front matter\n", encoding="utf-8")
    assert lookup.notes_for_video(video, notes) == [mine]


def test_renamed_video_still_finds_its_notes(tmp_path):
    video = write(tmp_path / "talk.mp4", b"v" * 100)
    md = notes_for(video, tmp_path / "notes" / "talk.md")
    renamed = tmp_path / "renamed copy.mp4"
    shutil.copy(video, renamed)
    assert lookup.notes_for_video(renamed, tmp_path / "notes") == [md]


def test_same_size_different_content_is_not_a_match(tmp_path):
    video = write(tmp_path / "a.mp4", b"a" * 100)
    notes_for(video, tmp_path / "notes" / "a.md")
    impostor = write(tmp_path / "b.mp4", b"b" * 100)
    assert lookup.notes_for_video(impostor, tmp_path / "notes") == []


def test_full_hash_decides_when_quick_hash_agrees(tmp_path):
    a = write(tmp_path / "a.mp4", b"\0" * (4 * MiB))
    notes_for(a, tmp_path / "notes" / "a.md")
    middle = bytearray(b"\0" * (4 * MiB))
    middle[2 * MiB] = 1
    b = write(tmp_path / "b.mp4", bytes(middle))
    assert quick_hash(a) == quick_hash(b)
    assert lookup.notes_for_video(b, tmp_path / "notes") == []


def test_old_notes_match_on_video_id(tmp_path, monkeypatch):
    video = write(tmp_path / "a.mp4", b"v")
    monkeypatch.setattr(lookup, "video_id", lambda p: (-42, "content"))
    md = tmp_path / "notes" / "old.md"
    md.parent.mkdir()
    md.write_text("---\nvideo_id: -42\n---\n# old\n", encoding="utf-8")
    assert lookup.notes_for_video(video, tmp_path / "notes") == [md]


# ---------- notes -> video ----------

def test_video_for_notes_searches_subfolders(tmp_path):
    raw = tmp_path / "raw"
    target = write(raw / "2026" / "nov" / "talk.mp4", b"t" * 500)
    write(raw / "same_size.mp4", b"x" * 500)          # same size, other content
    write(raw / "small.mp4", b"t" * 10)
    write(raw / "talk.txt", b"t" * 500)               # not a video
    md = notes_for(target, tmp_path / "notes" / "talk.md")
    assert lookup.video_for_notes(md, raw) == [target]


def test_video_for_notes_only_hashes_files_of_the_right_size(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    target = write(raw / "talk.mp4", b"t" * 500)
    for i in range(5):
        write(raw / f"other{i}.mp4", b"o" * (100 + i))
    md = notes_for(target, tmp_path / "notes" / "talk.md")
    hashed = []
    real = lookup.quick_hash
    monkeypatch.setattr(lookup, "quick_hash", lambda p: hashed.append(Path(p).name) or real(p))
    assert lookup.video_for_notes(md, raw) == [target]
    assert hashed == ["talk.mp4"]


def test_video_for_notes_without_fingerprint(tmp_path):
    md = tmp_path / "x.md"
    md.write_text("# no front matter\n", encoding="utf-8")
    with pytest.raises(ValueError):
        lookup.video_for_notes(md, tmp_path)


def test_unknown_hash_method_falls_back_to_full_hash(tmp_path):
    video = write(tmp_path / "a.mp4", b"v" * 50)
    md = notes_for(video, tmp_path / "notes" / "a.md",
                   quick_hash="0" * 64, quick_hash_method="some-future-method")
    assert lookup.notes_for_video(video, tmp_path / "notes") == [md]


# ---------- command line ----------

def test_cli_both_directions(tmp_path, capsys):
    raw = tmp_path / "raw"
    video = write(raw / "talk.mp4", b"t" * 200)
    md = notes_for(video, tmp_path / "notes" / "talk.md")

    assert lookup.main(["notes", str(video), "--in", str(tmp_path / "notes")]) == 0
    assert capsys.readouterr().out.strip() == str(md)
    assert lookup.main(["video", str(md), "--in", str(raw)]) == 0
    assert capsys.readouterr().out.strip() == str(video)


def test_cli_no_match(tmp_path, capsys):
    video = write(tmp_path / "talk.mp4", b"t")
    (tmp_path / "notes").mkdir()
    assert lookup.main(["notes", str(video), "--in", str(tmp_path / "notes")]) == 2
    assert "no match" in capsys.readouterr().out


def test_cli_missing_file(tmp_path, capsys):
    assert lookup.main(["notes", str(tmp_path / "nope.mp4")]) == 1
    assert "error:" in capsys.readouterr().err


def test_default_folders():
    root = Path(__file__).resolve().parents[1]
    assert lookup.NOTES_DIR == root / "data" / "meeting_notes"
    assert lookup.RAW_DIR == root / "data" / "raw"

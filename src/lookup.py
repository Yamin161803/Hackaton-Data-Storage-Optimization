#!/usr/bin/env python3
"""Find the notes for a video, or the video for a notes file, using the fingerprint at
the top of every notes file (file_size, quick_hash, full_hash; see videohash.py).

    python src/lookup.py notes "data/raw/talk.mp4"                 video -> its notes
    python src/lookup.py video "data/meeting_notes/talk.md"        notes -> its video
    python src/lookup.py video notes.md --in D:/archive            search another folder

Matching is cheapest first: the file size (free), then the quick hash (reads 2 MiB),
and the full hash only to confirm. Each hash is computed at most once per file, so
looking through a big folder of videos is fast: most files are ruled out by size.
A renamed file is still found; a re-encoded or edited video is not (different bytes).

Notes made before the fingerprint existed only have video_id; those are matched on
the content id instead (slower: reads the whole video through ffmpeg).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from notes.meeting_notes import read_front_matter
from videohash import (FULL_HASH_ALGORITHM, QUICK_HASH_METHOD, full_hash, quick_hash,
                       video_id)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
NOTES_DIR = DATA_DIR / "meeting_notes"
RAW_DIR = DATA_DIR / "raw"
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class _Video:
    """A video file whose hashes are computed only when first needed."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        self._quick = self._full = self._id = None

    @property
    def quick(self) -> str:
        if self._quick is None:
            self._quick = quick_hash(self.path)
        return self._quick

    @property
    def full(self) -> str:
        if self._full is None:
            self._full = full_hash(self.path)
        return self._full

    @property
    def content_id(self) -> str:
        if self._id is None:
            self._id = str(video_id(self.path)[0])
        return self._id


def matches(front: dict[str, str], video: _Video) -> bool:
    """Is `video` the file these notes (front matter) were made from?"""
    if "file_size" in front:
        if front["file_size"] != str(video.size):
            return False                                   # most files stop here
        if front.get("quick_hash") and front.get("quick_hash_method") == QUICK_HASH_METHOD:
            if front["quick_hash"] != video.quick:
                return False
        if front.get("full_hash") and front.get("full_hash_algorithm") == FULL_HASH_ALGORITHM:
            return front["full_hash"] == video.full         # confirmed
        if "quick_hash" in front:
            return front["quick_hash"] == video.quick
    if "video_id" in front:                                 # older notes
        return front["video_id"] == video.content_id
    return False


def notes_for_video(video_path: str | Path, notes_dir: str | Path = NOTES_DIR) -> list[Path]:
    """All notes in notes_dir that were made from this video file."""
    video = _Video(video_path)
    return [md for md in sorted(Path(notes_dir).glob("*.md"))
            if (front := read_front_matter(md)) and matches(front, video)]


def video_for_notes(md_path: str | Path, search_dir: str | Path = RAW_DIR) -> list[Path]:
    """All video files under search_dir (recursively) that these notes were made from."""
    front = read_front_matter(md_path)
    if not front:
        raise ValueError(f"{md_path} has no fingerprint at the top (made by an old version?)")
    found = []
    for path in sorted(Path(search_dir).rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        if "file_size" in front and path.stat().st_size != int(front["file_size"]):
            continue                                        # cheap skip, no hashing
        if matches(front, _Video(path)):
            found.append(path)
    return found


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Find notes for a video, or the video for notes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("notes", help="which notes belong to this video?")
    n.add_argument("video", type=Path)
    n.add_argument("--in", dest="folder", type=Path, default=NOTES_DIR,
                   help="notes folder (default: data/meeting_notes)")
    v = sub.add_parser("video", help="which video file are these notes from?")
    v.add_argument("notes", type=Path)
    v.add_argument("--in", dest="folder", type=Path, default=RAW_DIR,
                   help="folder to search, including subfolders (default: data/raw)")
    args = p.parse_args(argv)

    try:
        if args.cmd == "notes":
            found = notes_for_video(args.video, args.folder)
        else:
            found = video_for_notes(args.notes, args.folder)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not found:
        print(f"no match in {args.folder}")
        return 2
    for path in found:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

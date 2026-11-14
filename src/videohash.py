"""Hash functions for video: fingerprint (size + quick hash + full hash) and video id.

The fingerprint is written at the top of every notes file (data/meeting_notes), so the
video can be found from the notes and the notes from the video (see lookup.py):

    file_size            file size in bytes                 - free to compare
    quick_hash           SHA-256 of size + first and        - reads only 2 MiB, fast
                         last 1 MiB (QUICK_HASH_METHOD)
    full_hash            SHA-256 of the whole file          - exact, reads the whole file
                         (FULL_HASH_ALGORITHM)

Compare in that order: size, then quick hash, and full hash only to confirm.

The video id is the first 8 bytes of a SHA-256 of only the video and audio data
(metadata ignored) as a 64-bit integer, so it fits an INTEGER PRIMARY KEY in SQLite.
Older notes use it to recognise a video that was already processed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024  # read 1 MB at a time, so even big videos are fine

QUICK_HASH_BYTES = 1024 * 1024            # 1 MiB from the start and 1 MiB from the end
QUICK_HASH_METHOD = "sha256-size-first-last-1MiB"
FULL_HASH_ALGORITHM = "sha256"


@dataclass(frozen=True)
class Fingerprint:
    """Written at the top of the notes so the video can be found again."""
    file_size: int
    quick_hash: str
    full_hash: str
    quick_hash_method: str = QUICK_HASH_METHOD
    full_hash_algorithm: str = FULL_HASH_ALGORITHM


def _till_id(digest: bytes) -> int:
    """The first 8 bytes as a 64-bit integer (fits SQLite INTEGER)."""
    return int.from_bytes(digest[:8], "big", signed=True)


def _ffmpeg() -> str:
    """The system ffmpeg, otherwise the one bundled with imageio-ffmpeg."""
    from video_splitter.splitter import find_ffmpeg
    return find_ffmpeg()


def quick_hash(sokvag: str | Path) -> str:
    """
    Quick hash (hex): SHA-256 of the file size (8 bytes) + first 1 MiB + last 1 MiB.
    Reads at most 2 MiB however big the video is. Can in theory match for two files
    that differ only in the middle, so confirm with full_hash.
    """
    storlek = os.path.getsize(sokvag)
    h = hashlib.sha256(storlek.to_bytes(8, "big"))
    with open(sokvag, "rb") as f:
        h.update(f.read(QUICK_HASH_BYTES))
        if storlek > QUICK_HASH_BYTES:
            f.seek(max(QUICK_HASH_BYTES, storlek - QUICK_HASH_BYTES))
            h.update(f.read(QUICK_HASH_BYTES))
    return h.hexdigest()


def full_hash(sokvag: str | Path) -> str:
    """Full hash (hex): SHA-256 of the whole file. The same file always gives the same hash."""
    h = hashlib.new(FULL_HASH_ALGORITHM)
    with open(sokvag, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def fingerprint(sokvag: str | Path) -> Fingerprint:
    """Size, quick hash and full hash of a file."""
    return Fingerprint(os.path.getsize(sokvag), quick_hash(sokvag), full_hash(sokvag))


def hash_video(sokvag: str | Path) -> int:
    """
    Hash of the whole file as an id. Fast and exact: the same file always gives the same
    id. Changed metadata (e.g. the title inside the MP4 file) gives a different id.
    """
    return _till_id(bytes.fromhex(full_hash(sokvag)))


def hash_video_innehall(sokvag: str | Path) -> int:
    """
    Hash of only the video and audio data, without metadata.
    The same video with a different title/tags gives the same id.
    Needs ffmpeg (system or imageio-ffmpeg). The video is not re-encoded.
    """
    kommando = [
        _ffmpeg(), "-v", "error",
        "-i", str(sokvag),
        "-map", "0:v?", "-map", "0:a?",   # only video and audio streams
        "-map_metadata", "-1",            # drop all metadata
        "-c", "copy",                     # copy data without re-encoding
        "-f", "hash", "-hash", "sha256",  # let ffmpeg compute the hash
        "-",
    ]
    resultat = subprocess.run(kommando, capture_output=True, text=True, check=True)
    # ffmpeg prints e.g. "SHA256=3c8f..."
    hex_hash = resultat.stdout.strip().split("=", 1)[1]
    return _till_id(bytes.fromhex(hex_hash))


def video_id(sokvag: str | Path) -> tuple[int, str]:
    """
    (id, method) for a video. method = "content" (only video/audio, metadata ignored)
    or "file" (the whole file) if ffmpeg could not read the video.
    """
    try:
        return hash_video_innehall(sokvag), "content"
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError, IndexError,
            ValueError):
        return hash_video(sokvag), "file"

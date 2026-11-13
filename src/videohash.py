"""Hashfunktioner för video: fingeravtryck (storlek + snabb hash + full hash) och video-id.

Fingeravtrycket skrivs först i varje anteckning (data/meeting_notes), så att man kan hitta
videon från anteckningen och anteckningen från videon (se lookup.py):

    file_size            filstorlek i byte                  – gratis att jämföra
    quick_hash           SHA-256 av storlek + första och    – läser bara 2 MiB, snabb
                         sista 1 MiB (QUICK_HASH_METHOD)
    full_hash            SHA-256 av hela filen              – exakt, läser hela filen
                         (FULL_HASH_ALGORITHM)

Jämför i den ordningen: storlek, sedan snabb hash, och full hash bara för att bekräfta.

Video-id:t är de första 8 byten av en SHA-256 av bara video- och ljuddata (metadata
ignoreras) som ett 64-bitars heltal, så det passar som INTEGER PRIMARY KEY i SQLite.
Webbsidan använder det för att känna igen en video som redan har bearbetats.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024  # läser 1 MB åt gången, så även stora videor går bra

QUICK_HASH_BYTES = 1024 * 1024            # 1 MiB från början och 1 MiB från slutet
QUICK_HASH_METHOD = "sha256-size-first-last-1MiB"
FULL_HASH_ALGORITHM = "sha256"


@dataclass(frozen=True)
class Fingerprint:
    """Det som skrivs först i anteckningarna för att kunna hitta videon igen."""
    file_size: int
    quick_hash: str
    full_hash: str
    quick_hash_method: str = QUICK_HASH_METHOD
    full_hash_algorithm: str = FULL_HASH_ALGORITHM


def _till_id(digest: bytes) -> int:
    """De första 8 byten som ett 64-bitars heltal (passar SQLite INTEGER)."""
    return int.from_bytes(digest[:8], "big", signed=True)


def _ffmpeg() -> str:
    """ffmpeg från systemet, annars den som följer med imageio-ffmpeg."""
    from video_splitter.splitter import find_ffmpeg
    return find_ffmpeg()


def quick_hash(sokvag: str | Path) -> str:
    """
    Snabb hash (hex): SHA-256 av filstorleken (8 byte) + första 1 MiB + sista 1 MiB.
    Läser högst 2 MiB oavsett hur stor videon är. Kan i teorin vara lika för två filer
    som bara skiljer sig i mitten – bekräfta därför med full_hash.
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
    """Full hash (hex): SHA-256 av hela filen. Samma fil ger alltid samma hash."""
    h = hashlib.new(FULL_HASH_ALGORITHM)
    with open(sokvag, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def fingerprint(sokvag: str | Path) -> Fingerprint:
    """Storlek, snabb hash och full hash för en fil."""
    return Fingerprint(os.path.getsize(sokvag), quick_hash(sokvag), full_hash(sokvag))


def hash_video(sokvag: str | Path) -> int:
    """
    Hash av hela filen som id. Snabb och exakt: samma fil ger alltid samma id.
    Ändrad metadata (t.ex. titel inuti MP4-filen) ger ett annat id.
    """
    return _till_id(bytes.fromhex(full_hash(sokvag)))


def hash_video_innehall(sokvag: str | Path) -> int:
    """
    Hash av bara video- och ljuddata, utan metadata.
    Samma video med olika titel/taggar ger samma id.
    Kräver ffmpeg (systemets eller imageio-ffmpeg). Videon kodas inte om.
    """
    kommando = [
        _ffmpeg(), "-v", "error",
        "-i", str(sokvag),
        "-map", "0:v?", "-map", "0:a?",   # bara video- och ljudspår
        "-map_metadata", "-1",            # ta bort all metadata
        "-c", "copy",                     # kopiera data utan omkodning
        "-f", "hash", "-hash", "sha256",  # låt ffmpeg räkna ut hashen
        "-",
    ]
    resultat = subprocess.run(kommando, capture_output=True, text=True, check=True)
    # ffmpeg skriver t.ex. "SHA256=3c8f..."
    hex_hash = resultat.stdout.strip().split("=", 1)[1]
    return _till_id(bytes.fromhex(hex_hash))


def video_id(sokvag: str | Path) -> tuple[int, str]:
    """
    (id, metod) för en video. metod = "content" (bara video/ljud, metadata ignoreras)
    eller "file" (hela filen) om ffmpeg inte kunde läsa videon.
    """
    try:
        return hash_video_innehall(sokvag), "content"
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError, IndexError,
            ValueError):
        return hash_video(sokvag), "file"

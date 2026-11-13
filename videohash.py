"""Hashfunktioner för video"""
 
import hashlib
import subprocess
 
CHUNK = 1024 * 1024  # läser 1 MB åt gången, så även stora videor går bra
 
 
def _till_id(digest: bytes) -> int:
    """De första 8 byten som ett 64-bitars heltal (passar SQLite INTEGER)."""
    return int.from_bytes(digest[:8], "big", signed=True)
 
 
def hash_video(sokvag: str) -> int:
    """
    Hash av hela filen. Snabb och exakt: samma fil ger alltid samma id.
    Ändrad metadata (t.ex. titel inuti MP4-filen) ger ett annat id.
    """
    h = hashlib.sha256()
    with open(sokvag, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return _till_id(h.digest())
 
 
def hash_video_innehall(sokvag: str) -> int:
    """
    Hash av bara video- och ljuddata, utan metadata.
    Samma video med olika titel/taggar ger samma id.
    Kräver ffmpeg (sudo pacman -S ffmpeg). Videon kodas inte om.
    """
    kommando = [
        "ffmpeg", "-v", "error",
        "-i", sokvag,
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
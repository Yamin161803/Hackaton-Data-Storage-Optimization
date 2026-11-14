"""
Backend for the website: upload a video, get notes back.

Contains:
  - the URL /api/compress that function.js sends the file to
  - the /dedupe page (Rensa dubbletter) with the URLs in website/dedupe_api.py
  - ta_emot_fil()     - saves the uploaded file in a temporary folder
  - komprimera()      - makes notes of the video with the same pipeline as src/app.py
  - skicka_tillbaka() - sends the notes back to the page

A video is "compressed" into notes: frames + transcript, and optionally a summary and
search keywords (local AI model via Ollama).
The server saves them as usual in data/meeting_notes and data/resources. The downloaded
file has the frames embedded, so it works on its own.

The notes start with the file's fingerprint (size, quick hash, full hash; see
videohash.py). When the same file is uploaded again, the saved notes are found with it
(lookup.py) and sent straight back.

Choices on the page (form fields): summary=1/0, keywords=1/0, speakers=1/0 and
num_speakers=N (who is speaking). If the saved notes lack something that is chosen, the
video is processed again with the new choices.

Start (from the project folder):
    pip install flask
    python src/website/server.py            # or: cd src && python -m website
    python src/website/server.py --speakers --whisper-model large-v3
Then open http://localhost:8000 in the browser.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):  # run as a script: python src/website/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib.parse import quote  # noqa: E402

from flask import Flask, jsonify, request, send_file, send_from_directory  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

import app as pipeline  # noqa: E402  (src/app.py)
from lookup import notes_for_video  # noqa: E402
from notes.meeting_notes import (embed_local_images, has_summary,  # noqa: E402
                                 read_front_matter, read_keywords, read_speaker_count)
from videohash import Fingerprint, fingerprint  # noqa: E402
from website.dedupe_api import bp as dedupe_bp  # noqa: E402  (the Rensa dubbletter page)
from website.version_tree_api import bp as version_tree_bp  # noqa: E402  (version tree)
from website.i18n import msg  # noqa: E402

VIDEO_ENDELSER = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Folder with index.html, style.css and function.js
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Must match FIELD_NAME and MAX_FILE_MB in function.js
FIELD_NAME = "file"
MAX_FILE_MB = 2048

# Pipeline settings; changed with the flags in main().
# speakers/summary/keywords are defaults; the page chooses per video.
INSTALLNINGAR: dict = {"speakers": False, "whisper_model": "base", "summary": True,
                       "keywords": False}

# The GPU handles one video at a time
_PIPELINE_LAS = threading.Lock()

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024
app.register_blueprint(dedupe_bp)
app.register_blueprint(version_tree_bp)


# ------------------------------------------------------------------
# CORS - allows the page even when opened from a file or another port
# (e.g. Live Server). Replace "*" with your real address in production.
# ------------------------------------------------------------------
@app.after_request
def lagg_till_cors(svar):
    svar.headers["Access-Control-Allow-Origin"] = "*"
    svar.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    svar.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Lang"
    svar.headers["Access-Control-Expose-Headers"] = "Content-Disposition, X-File-Id, X-Cached, X-Warning"
    return svar


# ------------------------------------------------------------------
# Receive the file from the page
# ------------------------------------------------------------------
def ta_emot_fil(mapp: Path) -> tuple[Path, str]:
    """Save the uploaded file in `mapp`. Returns (path, original name)."""
    if FIELD_NAME not in request.files:
        raise ValueError(msg("no_file", field=FIELD_NAME))

    uppladdad = request.files[FIELD_NAME]
    if not uppladdad.filename:
        raise ValueError(msg("no_name"))

    filnamn = secure_filename(uppladdad.filename) or "video"
    sokvag = mapp / filnamn
    uppladdad.save(sokvag)  # streamed to disk, not the whole file in memory
    if sokvag.stat().st_size == 0:
        raise ValueError(msg("empty_file"))
    return sokvag, filnamn


@dataclass
class Val:
    """What the notes should contain."""
    summary: bool = True
    keywords: bool = False
    speakers: bool = False
    num_speakers: int | None = None


def _ja(falt: str, standard: bool) -> bool:
    varde = request.form.get(falt, "")
    return standard if varde == "" else varde.lower() in ("1", "true", "on", "yes")


def las_val() -> Val:
    """The choices from the page; missing fields get the server defaults (INSTALLNINGAR)."""
    val = Val(summary=_ja("summary", INSTALLNINGAR["summary"]),
              keywords=_ja("keywords", INSTALLNINGAR["keywords"]),
              speakers=_ja("speakers", INSTALLNINGAR["speakers"]))
    antal = request.form.get("num_speakers", "").strip()
    if antal:
        if not antal.isdigit() or not 1 <= int(antal) <= 20:
            raise ValueError(msg("bad_speakers"))
        val.speakers, val.num_speakers = True, int(antal)  # ett antal betyder talare
    return val


def ledigt_namn(stam: str, fp: Fingerprint) -> str:
    """The notes name. If another file already has this name, the hash is appended."""
    befintlig = pipeline.NOTES_DIR / f"{stam}.md"
    if befintlig.exists() and read_front_matter(befintlig).get("full_hash") != fp.full_hash:
        return f"{stam}_{fp.full_hash[:8]}"
    return stam


def saknas(md: Path, val: Val) -> list[str]:
    """What is chosen but missing from the saved notes."""
    fattas = []
    if val.summary and not has_summary(md):
        fattas.append("summary")
    if val.keywords and not read_keywords(md):
        fattas.append("keywords")
    if val.speakers:
        sparade = read_speaker_count(md)
        if sparade == 0 or (val.num_speakers and sparade != val.num_speakers):
            fattas.append("speakers")
    return fattas


# ------------------------------------------------------------------
# The video becomes notes
# ------------------------------------------------------------------
# warning message (website/i18n.py) for each part of the notes that could not be made
_VARNINGAR = {"summary": "warn_summary", "keywords": "warn_keywords",
              "speakers": "warn_speakers"}


def komprimera(sokvag: Path, fp: Fingerprint, val: Val) -> tuple[bytes, str, bool, str]:
    """
    Make notes of the video (or find the saved ones for the same file).
    Returns (notes_with_embedded_images, file_name, already_existed, warning).

    Saved notes are used if they have everything that is chosen; otherwise the video is
    processed again and the saved notes are replaced.
    """
    hittade = notes_for_video(sokvag, pipeline.NOTES_DIR)
    md = hittade[0] if hittade else None
    if md is not None and not saknas(md, val):
        fanns_redan = True
    else:
        stam = md.stem if md is not None else ledigt_namn(Path(sokvag).stem, fp)
        md = _gor_anteckningar(sokvag, stam, val)
        fanns_redan = False

    varning = " ".join(msg(_VARNINGAR[k]) for k in saknas(md, val))
    text = embed_local_images(md.read_text(encoding="utf-8"), md.parent)
    return text.encode("utf-8"), md.name, fanns_redan, varning


def _gor_anteckningar(sokvag: Path, stam: str, val: Val) -> Path:
    """Run the pipeline; the notes are saved as data/meeting_notes/<stam>.md."""
    video = sokvag.with_name(stam + sokvag.suffix)
    if video != sokvag:
        sokvag.rename(video)
    with _PIPELINE_LAS:
        return pipeline.make_notes(
            video, pipeline.NOTES_DIR / f"{stam}.md",
            whisper_model=INSTALLNINGAR["whisper_model"], summary=val.summary,
            keywords=val.keywords, speakers=val.speakers, num_speakers=val.num_speakers)


# ------------------------------------------------------------------
# Send the notes back
# ------------------------------------------------------------------
def skicka_tillbaka(data: bytes, filnamn: str, full_hash: str, fanns_redan: bool,
                    varning: str = ""):
    """Return the file as a download with its file name and the video's SHA-256."""
    svar = send_file(
        io.BytesIO(data),
        mimetype="text/markdown",
        as_attachment=True,
        download_name=filnamn,
    )
    svar.headers["X-File-Id"] = full_hash
    svar.headers["X-Cached"] = "1" if fanns_redan else "0"
    if varning:
        svar.headers["X-Warning"] = quote(varning)  # URL-encoded: headers only allow ASCII
    return svar


# ------------------------------------------------------------------
# The URL function.js calls (API_URL = "/api/compress")
# ------------------------------------------------------------------
@app.route("/api/compress", methods=["POST"])
def api_compress():
    mapp = Path(tempfile.mkdtemp(prefix="upload_"))
    try:
        try:
            sokvag, filnamn = ta_emot_fil(mapp)
        except ValueError as fel:
            return jsonify(detail=str(fel)), 400

        if sokvag.suffix.lower() not in VIDEO_ENDELSER:
            tillatna = ", ".join(sorted(VIDEO_ENDELSER))
            return jsonify(detail=msg("video_only", allowed=tillatna)), 415

        try:
            val = las_val()
        except ValueError as fel:
            return jsonify(detail=str(fel)), 400

        fp = fingerprint(sokvag)
        app.logger.info("Tog emot %s (%s byte, sha256 %s)", filnamn, fp.file_size,
                        fp.full_hash[:16])

        try:
            anteckningar, namn, fanns_redan, varning = komprimera(sokvag, fp, val)
        except Exception as fel:  # fel i pipelinen
            app.logger.exception("Anteckningarna kunde inte skapas")
            return jsonify(detail=msg("pipeline_failed", error=fel)), 500

        return skicka_tillbaka(anteckningar, namn, fp.full_hash, fanns_redan, varning)
    finally:
        shutil.rmtree(mapp, ignore_errors=True)  # the uploaded video is not kept


@app.errorhandler(413)
def for_stor_fil(_):
    return jsonify(detail=msg("too_large", mb=MAX_FILE_MB)), 413


# ------------------------------------------------------------------
# Serve the page from the same server, so CORS is not needed
# ------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/dedupe")
def dedupe_sida():
    return send_from_directory(STATIC_DIR, "dedupe.html")


@app.route("/versions")
def versions_sida():
    return send_from_directory(STATIC_DIR, "versions.html")


@app.route("/<path:namn>")
def statiska_filer(namn):
    if namn in ("index.html", "style.css", "function.js", "dedupe.html", "dedupe.js",
                "i18n.js", "versions.html", "versions.js"):
        return send_from_directory(STATIC_DIR, namn)
    return jsonify(detail=msg("not_found")), 404


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Website: video -> notes")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--speakers", action="store_true",
                   help="default: label who is speaking (can be changed on the page; needs "
                        "pyannote.audio + a Hugging Face token)")
    p.add_argument("--whisper-model", default="base",
                   help="tiny|base|small|medium|large-v3 (default: base)")
    p.add_argument("--no-summary", action="store_true",
                   help="default: no summary (can be changed on the page)")
    p.add_argument("--keywords", action="store_true",
                   help="default: add search keywords (can be changed on the page)")
    args = p.parse_args(argv)
    INSTALLNINGAR.update(speakers=args.speakers, whisper_model=args.whisper_model,
                         summary=not args.no_summary, keywords=args.keywords)
    # debug=False: the debug-mode reloader would restart during long runs
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

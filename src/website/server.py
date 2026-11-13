"""
Backend för webbsidan: ladda upp en video, få tillbaka anteckningar.

Innehåller:
  - URL:en /api/compress som function.js skickar filen till
  - sidan /dedupe (Rensa dubbletter) med URL:erna i website/dedupe_api.py
  - ta_emot_fil()     – sparar den uppladdade filen i en tillfällig mapp
  - komprimera()      – gör anteckningar av videon med samma pipeline som src/app.py
  - skicka_tillbaka() – skickar anteckningarna tillbaka till webbsidan

En video "komprimeras" till anteckningar: bildrutor + transkribering, och om man vill
en sammanfattning och sökord (lokal AI-modell via Ollama).
Servern sparar dem som vanligt i data/meeting_notes och data/resources. Filen som laddas
ner har bilderna inbakade, så den fungerar på egen hand.

Anteckningarna börjar med filens fingeravtryck (storlek, snabb hash, full hash; se
videohash.py). Laddas samma fil upp igen hittas de sparade anteckningarna med det
(lookup.py) och skickas direkt.

Val på sidan (formulärfält): summary=1/0 (sammanfattning), keywords=1/0 (sökord),
speakers=1/0 och num_speakers=N (vem som pratar). Saknar de sparade anteckningarna något
som väljs görs videon om med de nya valen.

Starta (från projektmappen):
    pip install flask
    python src/website/server.py            # eller: cd src && python -m website
    python src/website/server.py --speakers --whisper-model large-v3
Öppna sedan http://localhost:8000 i webbläsaren.
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

if __package__ in (None, ""):  # körs som skript: python src/website/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib.parse import quote  # noqa: E402

from flask import Flask, jsonify, request, send_file, send_from_directory  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

import app as pipeline  # noqa: E402  (src/app.py)
from lookup import notes_for_video  # noqa: E402
from notes.meeting_notes import (embed_local_images, has_summary,  # noqa: E402
                                 read_front_matter, read_keywords, read_speaker_count)
from videohash import Fingerprint, fingerprint  # noqa: E402
from website.dedupe_api import bp as dedupe_bp  # noqa: E402  (sidan Rensa dubbletter)

VIDEO_ENDELSER = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Mappen där index.html, style.css och function.js ligger
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Ska matcha FIELD_NAME och MAX_FILE_MB i function.js
FIELD_NAME = "file"
MAX_FILE_MB = 2048

# Inställningar för pipelinen; ändras med flaggorna i main().
# speakers/summary/keywords är förval; sidan väljer själv per video.
INSTALLNINGAR: dict = {"speakers": False, "whisper_model": "base", "summary": True,
                       "keywords": False}

# GPU:n klarar en video åt gången
_PIPELINE_LAS = threading.Lock()

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024
app.register_blueprint(dedupe_bp)


# ------------------------------------------------------------------
# CORS – tillåter sidan även när den öppnas från fil eller annan port
# (t.ex. Live Server). Byt "*" mot er riktiga adress i produktion.
# ------------------------------------------------------------------
@app.after_request
def lagg_till_cors(svar):
    svar.headers["Access-Control-Allow-Origin"] = "*"
    svar.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    svar.headers["Access-Control-Allow-Headers"] = "Content-Type"
    svar.headers["Access-Control-Expose-Headers"] = "Content-Disposition, X-File-Id, X-Cached, X-Warning"
    return svar


# ------------------------------------------------------------------
# Hämta filen från webbsidan
# ------------------------------------------------------------------
def ta_emot_fil(mapp: Path) -> tuple[Path, str]:
    """Sparar den uppladdade filen i `mapp`. Returnerar (sökväg, originalnamn)."""
    if FIELD_NAME not in request.files:
        raise ValueError(f"Ingen fil skickades i fältet '{FIELD_NAME}'.")

    uppladdad = request.files[FIELD_NAME]
    if not uppladdad.filename:
        raise ValueError("Filen saknar namn.")

    filnamn = secure_filename(uppladdad.filename) or "video"
    sokvag = mapp / filnamn
    uppladdad.save(sokvag)  # strömmas till disk, inte hela filen i minnet
    if sokvag.stat().st_size == 0:
        raise ValueError("Filen är tom.")
    return sokvag, filnamn


@dataclass
class Val:
    """Vad som ska finnas i anteckningarna."""
    summary: bool = True
    keywords: bool = False
    speakers: bool = False
    num_speakers: int | None = None


def _ja(falt: str, standard: bool) -> bool:
    varde = request.form.get(falt, "")
    return standard if varde == "" else varde.lower() in ("1", "true", "on", "yes")


def las_val() -> Val:
    """Valen från sidan; fält som saknas får serverns förval (INSTALLNINGAR)."""
    val = Val(summary=_ja("summary", INSTALLNINGAR["summary"]),
              keywords=_ja("keywords", INSTALLNINGAR["keywords"]),
              speakers=_ja("speakers", INSTALLNINGAR["speakers"]))
    antal = request.form.get("num_speakers", "").strip()
    if antal:
        if not antal.isdigit() or not 1 <= int(antal) <= 20:
            raise ValueError("Antal talare ska vara ett heltal mellan 1 och 20.")
        val.speakers, val.num_speakers = True, int(antal)  # ett antal betyder talare
    return val


def ledigt_namn(stam: str, fp: Fingerprint) -> str:
    """Namnet på anteckningarna. Har en annan fil redan samma namn läggs hashen till."""
    befintlig = pipeline.NOTES_DIR / f"{stam}.md"
    if befintlig.exists() and read_front_matter(befintlig).get("full_hash") != fp.full_hash:
        return f"{stam}_{fp.full_hash[:8]}"
    return stam


def saknas(md: Path, val: Val) -> list[str]:
    """Det som väljs men saknas i de sparade anteckningarna."""
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
# Videon blir anteckningar
# ------------------------------------------------------------------
_VARNINGAR = {
    "summary": "Ingen sammanfattning: kontrollera att Ollama körs (ollama serve).",
    "keywords": "Inga sökord: kontrollera att Ollama körs (ollama serve).",
    "speakers": "Det gick inte att avgöra vem som pratar. Kontrollera att pyannote.audio "
                "är installerat och att .hf_token finns i projektmappen.",
}


def komprimera(sokvag: Path, fp: Fingerprint, val: Val) -> tuple[bytes, str, bool, str]:
    """
    Gör anteckningar av videon (eller hittar de sparade för samma fil).
    Returnerar (anteckningar_med_inbakade_bilder, filnamn, fanns_redan, varning).

    Sparade anteckningar används om de har allt som väljs; annars görs videon om och
    de sparade anteckningarna ersätts.
    """
    hittade = notes_for_video(sokvag, pipeline.NOTES_DIR)
    md = hittade[0] if hittade else None
    if md is not None and not saknas(md, val):
        fanns_redan = True
    else:
        stam = md.stem if md is not None else ledigt_namn(Path(sokvag).stem, fp)
        md = _gor_anteckningar(sokvag, stam, val)
        fanns_redan = False

    varning = " ".join(_VARNINGAR[k] for k in saknas(md, val))
    text = embed_local_images(md.read_text(encoding="utf-8"), md.parent)
    return text.encode("utf-8"), md.name, fanns_redan, varning


def _gor_anteckningar(sokvag: Path, stam: str, val: Val) -> Path:
    """Kör pipelinen; anteckningarna sparas som data/meeting_notes/<stam>.md."""
    video = sokvag.with_name(stam + sokvag.suffix)
    if video != sokvag:
        sokvag.rename(video)
    with _PIPELINE_LAS:
        return pipeline.make_notes(
            video, pipeline.NOTES_DIR / f"{stam}.md",
            whisper_model=INSTALLNINGAR["whisper_model"], summary=val.summary,
            keywords=val.keywords, speakers=val.speakers, num_speakers=val.num_speakers)


# ------------------------------------------------------------------
# Skicka tillbaka anteckningarna
# ------------------------------------------------------------------
def skicka_tillbaka(data: bytes, filnamn: str, full_hash: str, fanns_redan: bool,
                    varning: str = ""):
    """Returnerar filen som nedladdning med filnamn och videons SHA-256."""
    svar = send_file(
        io.BytesIO(data),
        mimetype="text/markdown",
        as_attachment=True,
        download_name=filnamn,
    )
    svar.headers["X-File-Id"] = full_hash
    svar.headers["X-Cached"] = "1" if fanns_redan else "0"
    if varning:
        svar.headers["X-Warning"] = quote(varning)  # URL-kodad: headers tål bara ASCII
    return svar


# ------------------------------------------------------------------
# URL:en som function.js anropar (API_URL = "/api/compress")
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
            return jsonify(detail=f"Bara videofiler stöds ({tillatna})."), 415

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
            return jsonify(detail=f"Anteckningarna kunde inte skapas: {fel}"), 500

        return skicka_tillbaka(anteckningar, namn, fp.full_hash, fanns_redan, varning)
    finally:
        shutil.rmtree(mapp, ignore_errors=True)  # den uppladdade videon sparas inte


@app.errorhandler(413)
def for_stor_fil(_):
    return jsonify(detail=f"Filen är större än {MAX_FILE_MB} MB."), 413


# ------------------------------------------------------------------
# Serverar webbsidan från samma server, så CORS behövs inte
# ------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/dedupe")
def dedupe_sida():
    return send_from_directory(STATIC_DIR, "dedupe.html")


@app.route("/<path:namn>")
def statiska_filer(namn):
    if namn in ("index.html", "style.css", "function.js", "dedupe.html", "dedupe.js"):
        return send_from_directory(STATIC_DIR, namn)
    return jsonify(detail="Hittades inte."), 404


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Webbsida: video -> anteckningar")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--speakers", action="store_true",
                   help="förvalt: märk vem som pratar (kan ändras på sidan; kräver "
                        "pyannote.audio + Hugging Face-token)")
    p.add_argument("--whisper-model", default="base",
                   help="tiny|base|small|medium|large-v3 (default: base)")
    p.add_argument("--no-summary", action="store_true",
                   help="förvalt: ingen sammanfattning (kan ändras på sidan)")
    p.add_argument("--keywords", action="store_true",
                   help="förvalt: lägg till sökord (kan ändras på sidan)")
    args = p.parse_args(argv)
    INSTALLNINGAR.update(speakers=args.speakers, whisper_model=args.whisper_model,
                         summary=not args.no_summary, keywords=args.keywords)
    # debug=False: omstarten i debug-läge laddar om under långa körningar
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

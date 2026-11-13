"""
Backend för webbsidan: ladda upp en video, få tillbaka anteckningar.

Innehåller:
  - URL:en /api/compress som function.js skickar filen till
  - ta_emot_fil()     – sparar den uppladdade filen i en tillfällig mapp
  - komprimera()      – gör anteckningar av videon med samma pipeline som src/app.py
  - skicka_tillbaka() – skickar anteckningarna tillbaka till webbsidan

En video "komprimeras" till anteckningar: bildrutor + transkribering + sammanfattning.
Servern sparar dem som vanligt i data/meeting_notes och data/resources. Filen som laddas
ner har bilderna inbakade, så den fungerar på egen hand.

Varje video får ett id (hash av video- och ljuddata, se videohash.py) som skrivs i
anteckningarna. Laddas samma video upp igen skickas de sparade anteckningarna direkt.

På sidan kan man välja "Visa vem som pratar" (formulärfälten speakers=1 och
num_speakers=N). Har de sparade anteckningarna inga talare görs videon om med talare.

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
from pathlib import Path

if __package__ in (None, ""):  # körs som skript: python src/website/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urllib.parse import quote  # noqa: E402

from flask import Flask, jsonify, request, send_file, send_from_directory  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

import app as pipeline  # noqa: E402  (src/app.py)
from notes.meeting_notes import (embed_local_images, find_notes,  # noqa: E402
                                 read_speaker_count, read_video_id)
from videohash import video_id  # noqa: E402

VIDEO_ENDELSER = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Mappen där index.html, style.css och function.js ligger
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Ska matcha FIELD_NAME och MAX_FILE_MB i function.js
FIELD_NAME = "file"
MAX_FILE_MB = 2048

# Inställningar för pipelinen; ändras med flaggorna i main().
# "speakers" är standardvalet; sidan kan välja själv per video.
INSTALLNINGAR: dict = {"speakers": False, "whisper_model": "base", "summary": True}

# GPU:n klarar en video åt gången
_PIPELINE_LAS = threading.Lock()

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_MB * 1024 * 1024


# ------------------------------------------------------------------
# CORS – tillåter sidan även när den öppnas från fil eller annan port
# (t.ex. Live Server). Byt "*" mot er riktiga adress i produktion.
# ------------------------------------------------------------------
@app.after_request
def lagg_till_cors(svar):
    svar.headers["Access-Control-Allow-Origin"] = "*"
    svar.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
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


def las_talarval() -> tuple[bool, int | None]:
    """Valen från sidan: (visa vem som pratar, antal talare eller None)."""
    speakers = request.form.get("speakers", "")
    if speakers == "":
        valt = INSTALLNINGAR["speakers"]
    else:
        valt = speakers.lower() in ("1", "true", "on", "yes")
    antal = request.form.get("num_speakers", "").strip()
    if antal:
        if not antal.isdigit() or not 1 <= int(antal) <= 20:
            raise ValueError("Antal talare ska vara ett heltal mellan 1 och 20.")
        return True, int(antal)  # ett angivet antal betyder att talare ska visas
    return valt, None


def ledigt_namn(stam: str, fil_id: int) -> str:
    """Namnet på anteckningarna. Har en annan video redan samma namn läggs id:t till."""
    befintlig = pipeline.NOTES_DIR / f"{stam}.md"
    if befintlig.exists() and read_video_id(befintlig) != fil_id:
        return f"{stam}_{fil_id & 0xFFFFFFFF:08x}"
    return stam


# ------------------------------------------------------------------
# Videon blir anteckningar
# ------------------------------------------------------------------
def komprimera(sokvag: Path, fil_id: int, speakers: bool = False,
               num_speakers: int | None = None) -> tuple[bytes, str, bool, str]:
    """
    Gör anteckningar av videon (eller hittar de sparade för samma video-id).
    Returnerar (anteckningar_med_inbakade_bilder, filnamn, fanns_redan, varning).

    Sparade anteckningar används om de räcker: vill man ha talare men de sparade
    saknar talare (eller har ett annat antal än det angivna) görs videon om.
    """
    md = find_notes(pipeline.NOTES_DIR, fil_id)
    if md is not None and speakers:
        sparade = read_speaker_count(md)
        if sparade == 0 or (num_speakers and sparade != num_speakers):
            md = _gor_anteckningar(sokvag, md.stem, speakers, num_speakers)
            fanns_redan = False
        else:
            fanns_redan = True
    elif md is not None:
        fanns_redan = True
    else:
        md = _gor_anteckningar(sokvag, ledigt_namn(Path(sokvag).stem, fil_id),
                               speakers, num_speakers)
        fanns_redan = False

    varning = ""
    if speakers and read_speaker_count(md) == 0:
        varning = ("Det gick inte att avgöra vem som pratar. Kontrollera att "
                   "pyannote.audio är installerat och att .hf_token finns i projektmappen.")
    text = embed_local_images(md.read_text(encoding="utf-8"), md.parent)
    return text.encode("utf-8"), md.name, fanns_redan, varning


def _gor_anteckningar(sokvag: Path, stam: str, speakers: bool,
                      num_speakers: int | None) -> Path:
    """Kör pipelinen; anteckningarna sparas som data/meeting_notes/<stam>.md."""
    video = sokvag.with_name(stam + sokvag.suffix)
    if video != sokvag:
        sokvag.rename(video)
    with _PIPELINE_LAS:
        return pipeline.make_notes(
            video, pipeline.NOTES_DIR / f"{stam}.md",
            whisper_model=INSTALLNINGAR["whisper_model"], summary=INSTALLNINGAR["summary"],
            speakers=speakers, num_speakers=num_speakers)


# ------------------------------------------------------------------
# Skicka tillbaka anteckningarna
# ------------------------------------------------------------------
def skicka_tillbaka(data: bytes, filnamn: str, fil_id: int, fanns_redan: bool,
                    varning: str = ""):
    """Returnerar filen som nedladdning med filnamn och videons hash-id."""
    svar = send_file(
        io.BytesIO(data),
        mimetype="text/markdown",
        as_attachment=True,
        download_name=filnamn,
    )
    svar.headers["X-File-Id"] = str(fil_id)
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
            speakers, num_speakers = las_talarval()
        except ValueError as fel:
            return jsonify(detail=str(fel)), 400

        fil_id, metod = video_id(sokvag)
        app.logger.info("Tog emot %s med id %s (%s)", filnamn, fil_id, metod)

        try:
            anteckningar, namn, fanns_redan, varning = komprimera(
                sokvag, fil_id, speakers, num_speakers)
        except Exception as fel:  # fel i pipelinen
            app.logger.exception("Anteckningarna kunde inte skapas")
            return jsonify(detail=f"Anteckningarna kunde inte skapas: {fel}"), 500

        return skicka_tillbaka(anteckningar, namn, fil_id, fanns_redan, varning)
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


@app.route("/<path:namn>")
def statiska_filer(namn):
    if namn in ("style.css", "function.js"):
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
    p.add_argument("--no-summary", action="store_true", help="ingen Ollama-sammanfattning")
    args = p.parse_args(argv)
    INSTALLNINGAR.update(speakers=args.speakers, whisper_model=args.whisper_model,
                         summary=not args.no_summary)
    # debug=False: omstarten i debug-läge laddar om under långa körningar
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

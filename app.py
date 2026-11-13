"""
Backend för filkomprimeringssidan.

Innehåller:
  - URL:en /api/compress som function.js skickar filen till
  - ta_emot_fil()   – hämtar den uppladdade filen från anropet
  - skicka_tillbaka() – skickar den komprimerade filen tillbaka till webbsidan

Själva komprimeringen skriver du själv i funktionen komprimera() längre ned.

Starta:
    pip install flask
    python app.py
Öppna sedan http://localhost:8000 i webbläsaren.
"""

import hashlib
import io
import os
import subprocess
import tempfile

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from videohash import hash_video, hash_video_innehall

VIDEO_ENDELSER = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

# Mappen där index.html, style.css och function.js ligger
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))

# Ska matcha FIELD_NAME och MAX_FILE_MB i function.js
FIELD_NAME = "file"
MAX_FILE_MB = 200

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
    svar.headers["Access-Control-Expose-Headers"] = "Content-Disposition, X-File-Id"
    return svar


# ------------------------------------------------------------------
# Din komprimering – ersätt innehållet med din egen kod
# ------------------------------------------------------------------
def komprimera(data: bytes, filnamn: str) -> tuple[bytes, str]:
    """
    Tar emot originalfilens innehåll och namn.
    Ska returnera (komprimerad_data, nytt_filnamn).
    """
    raise NotImplementedError("Komprimeringsfunktionen är inte inkopplad ännu.")


# ------------------------------------------------------------------
# Hämta filen från webbsidan
# ------------------------------------------------------------------
def ta_emot_fil() -> tuple[bytes, str]:
    """Läser den uppladdade filen från multipart/form-data."""
    if FIELD_NAME not in request.files:
        raise ValueError(f"Ingen fil skickades i fältet '{FIELD_NAME}'.")

    uppladdad = request.files[FIELD_NAME]
    if not uppladdad.filename:
        raise ValueError("Filen saknar namn.")

    filnamn = secure_filename(uppladdad.filename) or "fil"
    data = uppladdad.read()
    if not data:
        raise ValueError("Filen är tom.")

    return data, filnamn


# ------------------------------------------------------------------
# Hashnummer (id) för den uppladdade filen
# ------------------------------------------------------------------
def rakna_hash(data: bytes, filnamn: str) -> int:
    """
    Video: hash av bara video- och ljuddata (metadata ignoreras) via videohash.
    Faller tillbaka på hash av hela filen om ffmpeg saknas eller misslyckas.
    Andra filer: SHA-256 av hela innehållet. Alla id:n är 8-byte heltal.
    """
    andelse = os.path.splitext(filnamn)[1].lower()
    if andelse not in VIDEO_ENDELSER:
        return int.from_bytes(hashlib.sha256(data).digest()[:8], "big", signed=True)

    # videohash arbetar med filer, så videon skrivs till en tillfällig fil
    with tempfile.NamedTemporaryFile(suffix=andelse, delete=False) as tmp:
        tmp.write(data)
        tmp_sokvag = tmp.name
    try:
        try:
            return hash_video_innehall(tmp_sokvag)
        except (FileNotFoundError, subprocess.CalledProcessError, IndexError, ValueError):
            app.logger.warning("ffmpeg kunde inte användas, hashar hela filen")
            return hash_video(tmp_sokvag)
    finally:
        os.remove(tmp_sokvag)


# ------------------------------------------------------------------
# Skicka tillbaka den komprimerade filen
# ------------------------------------------------------------------
def skicka_tillbaka(data: bytes, filnamn: str, fil_id: int):
    """Returnerar filen som nedladdning med filnamn och originalfilens hash-id."""
    svar = send_file(
        io.BytesIO(data),
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=filnamn,
    )
    svar.headers["X-File-Id"] = str(fil_id)
    return svar


# ------------------------------------------------------------------
# URL:en som function.js anropar (API_URL = "/api/compress")
# ------------------------------------------------------------------
@app.route("/api/compress", methods=["POST"])
def api_compress():
    try:
        data, filnamn = ta_emot_fil()
    except ValueError as fel:
        return jsonify(detail=str(fel)), 400

    fil_id = rakna_hash(data, filnamn)
    app.logger.info("Tog emot %s med id %s", filnamn, fil_id)

    try:
        komprimerad, nytt_namn = komprimera(data, filnamn)
    except NotImplementedError as fel:
        return jsonify(detail=str(fel)), 501
    except Exception as fel:  # fel i din komprimering
        app.logger.exception("Komprimeringen misslyckades")
        return jsonify(detail=f"Komprimeringen misslyckades: {fel}"), 500

    return skicka_tillbaka(komprimerad, nytt_namn, fil_id)


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
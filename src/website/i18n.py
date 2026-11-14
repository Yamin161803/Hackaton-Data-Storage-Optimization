"""Server messages in English (default) and Swedish.

The page sends its language in the X-Lang header ("en" or "sv"); anything else means
English. Use msg("key", name=value) inside a request.
"""
from __future__ import annotations

from flask import has_request_context, request

DEFAULT_LANG = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        # upload page (server.py)
        "no_file": "No file was sent in the field '{field}'.",
        "no_name": "The file has no name.",
        "empty_file": "The file is empty.",
        "bad_speakers": "Number of speakers must be a whole number from 1 to 20.",
        "video_only": "Only video files are supported ({allowed}).",
        "pipeline_failed": "The notes could not be created: {error}",
        "too_large": "The file is larger than {mb} MB.",
        "not_found": "Not found.",
        "warn_summary": "No summary: check that Ollama is running (ollama serve).",
        "warn_keywords": "No keywords: check that Ollama is running (ollama serve).",
        "warn_speakers": "Could not tell who is speaking. Check that pyannote.audio is "
                         "installed and that .hf_token is in the project folder.",
        # remove duplicates page (dedupe_api.py)
        "no_folder": "No folder given.",
        "folder_missing": "The folder does not exist: {path}",
        "bad_trash": "The folder {path} cannot be used as a trash folder.",
        "trash_inside_target": "The trash folder cannot be the folder being cleaned or a "
                               "folder above it.",
        "confirm_delete": "Deleting must be confirmed.",
        "trash_changed": "The trash folder has changed since you confirmed. Confirm again.",
        "no_permission": "No permission to read {path}.",
        "bad_mode": "The mode must be 'batch' or 'online'.",
        "busy": "A cleanup is already running. Wait until it is done.",
        "home": "Home",
    },
    "sv": {
        "no_file": "Ingen fil skickades i fältet '{field}'.",
        "no_name": "Filen saknar namn.",
        "empty_file": "Filen är tom.",
        "bad_speakers": "Antal talare ska vara ett heltal mellan 1 och 20.",
        "video_only": "Bara videofiler stöds ({allowed}).",
        "pipeline_failed": "Anteckningarna kunde inte skapas: {error}",
        "too_large": "Filen är större än {mb} MB.",
        "not_found": "Hittades inte.",
        "warn_summary": "Ingen sammanfattning: kontrollera att Ollama körs (ollama serve).",
        "warn_keywords": "Inga sökord: kontrollera att Ollama körs (ollama serve).",
        "warn_speakers": "Det gick inte att avgöra vem som pratar. Kontrollera att "
                         "pyannote.audio är installerat och att .hf_token finns i "
                         "projektmappen.",
        "no_folder": "Ingen mapp angiven.",
        "folder_missing": "Mappen finns inte: {path}",
        "bad_trash": "Mappen {path} kan inte användas som papperskorg.",
        "trash_inside_target": "Papperskorgen får inte vara mappen som rensas eller en mapp "
                               "ovanför den.",
        "confirm_delete": "Radering måste bekräftas.",
        "trash_changed": "Papperskorgen har ändrats sedan du bekräftade. Bekräfta igen.",
        "no_permission": "Ingen behörighet att läsa {path}.",
        "bad_mode": "Läget ska vara 'batch' eller 'online'.",
        "busy": "En rensning pågår redan. Vänta tills den är klar.",
        "home": "Hem",
    },
}


def lang() -> str:
    """The language of the current request: "sv" when the page asks for it, else "en"."""
    if has_request_context():
        asked = request.headers.get("X-Lang", "").strip().lower()[:2]
        if asked in MESSAGES:
            return asked
    return DEFAULT_LANG


def msg(key: str, **values) -> str:
    """The message `key` in the request's language, with {placeholders} filled in."""
    text = MESSAGES[lang()].get(key) or MESSAGES[DEFAULT_LANG][key]
    return text.format(**values)

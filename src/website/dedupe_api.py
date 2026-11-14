"""
Backend for the "Rensa dubbletter" (remove duplicates) page, dedupe.html: runs
src/dedupe/slide_dedupe.py on a folder and moves (and optionally deletes) the files that
are not needed.

URLs:
  GET  /api/dedupe/defaults          default folders and supported file types
  GET  /api/dedupe/browse?path=...   folders to choose from (the folder picker on the page)
  GET  /api/dedupe/trash?path=...    number of files and size of the trash folder
  POST /api/dedupe/run               runs the cleanup, answers with the report (time, space)
  POST /api/dedupe/empty             empties the trash folder

Deleting requires the page to send the number of files and the size the user approved.
If the folder's contents changed since then, nothing is deleted (status 409), so what
gets deleted is always what was shown in the confirmation.

Every run uses a new, temporary index: the folder is always analysed in full, so a dry
run followed by a real run gives the same result.
"""
from __future__ import annotations

import json
import os
import string
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, jsonify, request

from dedupe.slide_dedupe import SUPPORTED, analyze, collect_files
from website.i18n import msg

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_TARGET = DATA_DIR / "raw"
DEFAULT_TRASH = DATA_DIR / "trash"

bp = Blueprint("dedupe", __name__)
_KOR_LAS = threading.Lock()  # one cleanup at a time


class Fel(Exception):
    """An error shown to the user, with an HTTP status."""

    def __init__(self, text: str, status: int = 400, **extra):
        super().__init__(text)
        self.status, self.extra = status, extra


@bp.errorhandler(Fel)
def _visa_fel(fel: Fel):
    return jsonify(detail=str(fel), **fel.extra), fel.status


# ------------------------------------------------------------------
# Paths and safety
# ------------------------------------------------------------------
def _mapp(text: str | None, standard: Path | None = None, ska_finnas: bool = True) -> Path:
    text = (text or "").strip().strip('"')
    if not text:
        if standard is None:
            raise Fel(msg("no_folder"))
        return standard.resolve()
    p = Path(os.path.expandvars(os.path.expanduser(text)))
    if not p.is_absolute():
        p = PROJECT_DIR / p
    p = p.resolve()
    if ska_finnas and not p.is_dir():
        raise Fel(msg("folder_missing", path=p), 404)
    return p


def _inuti(barn: Path, foralder: Path) -> bool:
    return barn == foralder or foralder in barn.parents


def _kontrollera_papperskorg(trash: Path, target: Path | None = None) -> None:
    """Refuse folders that must never be emptied: drive roots, the home folder, the project, etc."""
    skyddade = {Path(trash.anchor), Path.home().resolve(), PROJECT_DIR, DATA_DIR,
                PROJECT_DIR / "src", PROJECT_DIR / "tests", PROJECT_DIR / "docs"}
    if trash in skyddade or _inuti(PROJECT_DIR, trash) or _inuti(Path.home().resolve(), trash):
        raise Fel(msg("bad_trash", path=trash))
    if target is not None and _inuti(target, trash):
        raise Fel(msg("trash_inside_target"))


def _innehall(mapp: Path) -> tuple[int, int]:
    """(number of files, total size in bytes), all subfolders included."""
    antal = storlek = 0
    if mapp.is_dir():
        for f in mapp.rglob("*"):
            if f.is_file() and not f.is_symlink():
                antal += 1
                storlek += f.stat().st_size
    return antal, storlek


def _info(mapp: Path) -> dict:
    antal, storlek = _innehall(mapp)
    return {"path": str(mapp), "name": mapp.name or str(mapp), "exists": mapp.is_dir(),
            "files": antal, "bytes": storlek}


def _godkant(mapp: Path, data: dict) -> None:
    """The contents must be exactly what the user approved in the confirmation."""
    nu = _info(mapp)
    try:
        godkant = (int(data["files"]), int(data["bytes"]))
    except (KeyError, TypeError, ValueError):
        raise Fel(msg("confirm_delete"), 400, trash=nu) from None
    if godkant != (nu["files"], nu["bytes"]):
        raise Fel(msg("trash_changed"), 409, trash=nu)


def _tom(mapp: Path) -> tuple[int, int]:
    """Delete everything in the folder (but not the folder). Returns (files, bytes)."""
    antal = storlek = 0
    for f in sorted(mapp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if f.is_symlink() or f.is_file():
            if f.is_file():
                antal += 1
                storlek += f.stat().st_size
            f.unlink()
        elif f.is_dir():
            f.rmdir()  # empty now, its contents were removed first
    return antal, storlek


# ------------------------------------------------------------------
# URLs
# ------------------------------------------------------------------
@bp.route("/api/dedupe/defaults")
def standardval():
    return jsonify(target=str(DEFAULT_TARGET), trash=str(DEFAULT_TRASH),
                   extensions=sorted(SUPPORTED))


@bp.route("/api/dedupe/browse")
def bladdra():
    text = request.args.get("path", "")
    if not text:
        start = DEFAULT_TARGET if DEFAULT_TARGET.is_dir() else DATA_DIR
        mapp = start if start.is_dir() else PROJECT_DIR
    else:
        mapp = _mapp(text)
    mappar = []
    try:
        for d in sorted(mapp.iterdir(), key=lambda p: p.name.lower()):
            if d.is_dir() and not d.name.startswith((".", "$")):
                mappar.append({"name": d.name, "path": str(d)})
    except PermissionError:
        raise Fel(msg("no_permission", path=mapp), 403) from None
    filer = sum(1 for f in mapp.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED)
    rotter = ([f"{b}:\\" for b in string.ascii_uppercase if os.path.isdir(f"{b}:\\")]
              if os.name == "nt" else ["/"])
    return jsonify(path=str(mapp), parent=str(mapp.parent) if mapp.parent != mapp else None,
                   dirs=mappar, supported_files_here=filer, roots=rotter,
                   shortcuts=[{"name": "data", "path": str(DATA_DIR)},
                              {"name": msg("home"), "path": str(Path.home())}])


@bp.route("/api/dedupe/trash")
def papperskorg():
    trash = _mapp(request.args.get("path"), DEFAULT_TRASH, ska_finnas=False)
    _kontrollera_papperskorg(trash)  # also avoids counting through e.g. the whole home folder
    return jsonify(_info(trash))


@bp.route("/api/dedupe/empty", methods=["POST"])
def tom_papperskorg():
    data = request.get_json(silent=True) or {}
    trash = _mapp(data.get("path"), ska_finnas=True)
    _kontrollera_papperskorg(trash)
    with _KOR_LAS:
        _godkant(trash, data)
        antal, storlek = _tom(trash)
    return jsonify(deleted_files=antal, freed_bytes=storlek, trash=_info(trash))


@bp.route("/api/dedupe/run", methods=["POST"])
def kor():
    data = request.get_json(silent=True) or {}
    target = _mapp(data.get("target"))
    flytta = bool(data.get("move"))
    radera = flytta and bool(data.get("auto_delete"))
    mode = data.get("mode", "batch")
    if mode not in ("batch", "online"):
        raise Fel(msg("bad_mode"))

    trash = None
    if flytta:
        trash = _mapp(data.get("trash"), ska_finnas=False)
        _kontrollera_papperskorg(trash, target)

    if not _KOR_LAS.acquire(blocking=False):
        raise Fel(msg("busy"), 409)
    try:
        if radera:
            _godkant(trash, data.get("confirm") or {})
        return jsonify(_kor_rensning(target, trash, radera, mode, bool(data.get("fast_images"))))
    finally:
        _KOR_LAS.release()


def _kor_rensning(target: Path, trash: Path | None, radera: bool, mode: str,
                  fast_images: bool) -> dict:
    t0 = time.perf_counter()
    filer = [f for f in collect_files([str(target)])
             if trash is None or not _inuti(f.resolve(), trash)]  # not what is already in the trash
    with tempfile.TemporaryDirectory(prefix="dedupe_") as tmp:
        args = SimpleNamespace(
            inputs=[str(f) for f in filer], index=str(Path(tmp) / "index.json"), mode=mode,
            trash=str(trash) if trash else None, text_only=False, keep_page_numbers=False,
            fast_images=fast_images, no_prefilter=False, workers=1, report_json=None,
            profile=None, snakeviz=False, top=10, quiet=True)
        report, _, _ = (analyze(args) if filer else (_tom_rapport(mode, args.trash), None, None))
    report = json.loads(json.dumps(report))  # only JSON types (no Path/set)

    raderat = None
    if radera and trash is not None and trash.is_dir():
        t = time.perf_counter()
        antal, storlek = _tom(trash)
        raderat = {"files": antal, "bytes": storlek, "seconds": time.perf_counter() - t}

    report["web"] = {
        "target": str(target), "trash": str(trash) if trash else None, "moved": trash is not None,
        "auto_delete": radera, "deleted": raderat,
        "total_seconds": time.perf_counter() - t0,
        "trash_after": _info(trash) if trash else None,
    }
    return report


def _tom_rapport(mode: str, trash: str | None) -> dict:
    """The report when the folder has no PDF/PPTX/image files."""
    return {
        "settings": {"mode": mode, "trash": trash},
        "files": {"found": 0, "already_indexed": 0, "new": 0, "duplicates_by_content": 0,
                  "parsed": 0, "errors": 0},
        "space": {"removed_this_run_files": 0, "removed_this_run_bytes": 0, "kept_files": 0,
                  "kept_bytes": 0, "saved_percent": 0.0, "applied": bool(trash)},
        "slides": {"unique": 0, "parsed_this_run": 0},
        "time": {"total_seconds": 0.0, "stages_seconds": {}, "avg_ms_per_slide": 0.0,
                 "parse_mb_per_second": 0.0, "est_seconds_saved_by_prefilter": 0.0},
        "prefilter": {}, "phases": {}, "slowest_files": [], "hints": [],
        "removed": [], "kept": [], "moved_files": 0,
    }

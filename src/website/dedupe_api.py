"""
Backend för sidan "Rensa dubbletter" (dedupe.html): kör src/dedupe/slide_dedupe.py på en
mapp och flyttar (och om man vill raderar) filerna som inte behövs.

URL:er:
  GET  /api/dedupe/browse?path=...   mappar att välja bland (mappväljaren på sidan)
  GET  /api/dedupe/trash?path=...    antal filer och storlek i papperskorgsmappen
  POST /api/dedupe/run               kör rensningen, svarar med rapporten (tid, utrymme)
  POST /api/dedupe/empty             tömmer papperskorgsmappen

Radering kräver att sidan skickar med det antal filer och den storlek som användaren
godkände. Har mappens innehåll ändrats sedan dess raderas inget (svar 409), så det som
raderas är alltid det som visades i frågan.

Varje körning använder ett nytt, tillfälligt index: mappen analyseras alltid i sin helhet,
så en provkörning följd av en riktig körning ger samma resultat.
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

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_TARGET = DATA_DIR / "raw"
DEFAULT_TRASH = DATA_DIR / "trash"
_DB_PATH = DATA_DIR / "database"          # corpus store (cache, persists across runs)
_OBJECTS_DIR = DATA_DIR / "objects"       # content-addressed image blobs

bp = Blueprint("dedupe", __name__)
_KOR_LAS = threading.Lock()  # en rensning åt gången


class Fel(Exception):
    """Fel som visas för användaren, med HTTP-status."""

    def __init__(self, text: str, status: int = 400, **extra):
        super().__init__(text)
        self.status, self.extra = status, extra


@bp.errorhandler(Fel)
def _visa_fel(fel: Fel):
    return jsonify(detail=str(fel), **fel.extra), fel.status


# ------------------------------------------------------------------
# Sökvägar och säkerhet
# ------------------------------------------------------------------
def _mapp(text: str | None, standard: Path | None = None, ska_finnas: bool = True) -> Path:
    text = (text or "").strip().strip('"')
    if not text:
        if standard is None:
            raise Fel("Ingen mapp angiven.")
        return standard.resolve()
    p = Path(os.path.expandvars(os.path.expanduser(text)))
    if not p.is_absolute():
        p = PROJECT_DIR / p
    p = p.resolve()
    if ska_finnas and not p.is_dir():
        raise Fel(f"Mappen finns inte: {p}", 404)
    return p


def _inuti(barn: Path, foralder: Path) -> bool:
    return barn == foralder or foralder in barn.parents


def _kontrollera_papperskorg(trash: Path, target: Path | None = None) -> None:
    """Stoppar mappar som aldrig får tömmas: rotkataloger, hemkatalogen, projektet m.m."""
    skyddade = {Path(trash.anchor), Path.home().resolve(), PROJECT_DIR, DATA_DIR,
                PROJECT_DIR / "src", PROJECT_DIR / "tests", PROJECT_DIR / "docs"}
    if trash in skyddade or _inuti(PROJECT_DIR, trash) or _inuti(Path.home().resolve(), trash):
        raise Fel(f"Mappen {trash} kan inte användas som papperskorg.")
    if target is not None and _inuti(target, trash):
        raise Fel("Papperskorgen får inte vara mappen som rensas eller en mapp ovanför den.")


def _innehall(mapp: Path) -> tuple[int, int]:
    """(antal filer, total storlek i byte), alla undermappar inräknade."""
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
    """Innehållet ska vara exakt det användaren godkände i frågan."""
    nu = _info(mapp)
    try:
        godkant = (int(data["files"]), int(data["bytes"]))
    except (KeyError, TypeError, ValueError):
        raise Fel("Radering måste bekräftas.", 400, trash=nu) from None
    if godkant != (nu["files"], nu["bytes"]):
        raise Fel("Papperskorgen har ändrats sedan du bekräftade. Bekräfta igen.", 409, trash=nu)


def _tom(mapp: Path) -> tuple[int, int]:
    """Raderar allt i mappen (men inte mappen). Returnerar (antal filer, byte)."""
    antal = storlek = 0
    for f in sorted(mapp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if f.is_symlink() or f.is_file():
            if f.is_file():
                antal += 1
                storlek += f.stat().st_size
            f.unlink()
        elif f.is_dir():
            f.rmdir()  # tom nu, innehållet togs bort först
    return antal, storlek


# ------------------------------------------------------------------
# URL:er
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
        raise Fel(f"Ingen behörighet att läsa {mapp}.", 403) from None
    filer = sum(1 for f in mapp.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED)
    rotter = ([f"{b}:\\" for b in string.ascii_uppercase if os.path.isdir(f"{b}:\\")]
              if os.name == "nt" else ["/"])
    return jsonify(path=str(mapp), parent=str(mapp.parent) if mapp.parent != mapp else None,
                   dirs=mappar, supported_files_here=filer, roots=rotter,
                   shortcuts=[{"name": "data", "path": str(DATA_DIR)},
                              {"name": "Hem", "path": str(Path.home())}])


@bp.route("/api/dedupe/trash")
def papperskorg():
    trash = _mapp(request.args.get("path"), DEFAULT_TRASH, ska_finnas=False)
    _kontrollera_papperskorg(trash)  # också för att inte räkna igenom t.ex. hela hemkatalogen
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
        raise Fel("Läget ska vara 'batch' eller 'online'.")

    trash = None
    if flytta:
        trash = _mapp(data.get("trash"), ska_finnas=False)
        _kontrollera_papperskorg(trash, target)

    if not _KOR_LAS.acquire(blocking=False):
        raise Fel("En rensning pågår redan. Vänta tills den är klar.", 409)
    try:
        if radera:
            _godkant(trash, data.get("confirm") or {})
        return jsonify(_kor_rensning(target, trash, radera, mode, bool(data.get("fast_images"))))
    finally:
        _KOR_LAS.release()


@bp.route("/api/store/status")
def store_status():
    """How many documents/pages/images are cached in the store."""
    from store import Store
    s = Store(_DB_PATH, objects_dir=_OBJECTS_DIR)
    try:
        docs = s.conn.execute("SELECT COUNT(*) FROM document").fetchone()[0]
        pages = s.conn.execute("SELECT COUNT(*) FROM page").fetchone()[0]
        images = s.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0]
    finally:
        s.close()
    return jsonify(documents=docs, pages=pages, images=images)


@bp.route("/api/store/reset", methods=["POST"])
def store_reset():
    """DESTRUCTIVE: wipe the whole corpus store (documents/pages/images + blobs)."""
    from store import Store
    with _KOR_LAS:                      # don't reset while a run ingests
        s = Store(_DB_PATH, objects_dir=_OBJECTS_DIR)
        try:
            removed = s.reset()
        finally:
            s.close()
    return jsonify(reset=True, removed=removed)


def _kor_rensning(target: Path, trash: Path | None, radera: bool, mode: str,
                  fast_images: bool) -> dict:
    t0 = time.perf_counter()
    filer = [f for f in collect_files([str(target)])
             if trash is None or not _inuti(f.resolve(), trash)]  # inte det som redan slängts
    with tempfile.TemporaryDirectory(prefix="dedupe_") as tmp:
        args = SimpleNamespace(
            inputs=[str(f) for f in filer], index=str(Path(tmp) / "index.json"), mode=mode,
            trash=str(trash) if trash else None, text_only=False, keep_page_numbers=False,
            fast_images=fast_images, no_prefilter=False, workers=1, report_json=None,
            profile=None, snakeviz=False, top=10, quiet=True)
        report, _, _ = (analyze(args) if filer else (_tom_rapport(mode, args.trash), None, None))
    report = json.loads(json.dumps(report))  # bara JSON-typer (inga Path/set)

    # Ingest PDF/PPTX into the persistent corpus store so version families build
    # up across runs (the store is the cache). Collect any file that is a new
    # version belonging to a family (>1 node) so the page can show its tree.
    report["version_trees"], report["images"] = _ingest_into_store(filer)

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


def _ingest_into_store(files: list[Path]) -> tuple[list[dict], dict]:
    """Ingest PDF/PPTX files into the corpus store (the cache).

    Returns (trees, images) where:
      trees  = list of {doc_id, name, status, family_size} for files that end up
               in a VERSION FAMILY (>1 node) so the page can render its tree.
      images = {"total": N, "unique": M} image counts across the whole store
               (total occurrences before dedup vs unique pictures stored once).
    """
    from store import Store, SOURCE_PDF, SOURCE_PPTX
    from store.ingest_files import ingest_file
    from store.version_tree import version_family

    trees = []
    seen_families: set[int] = set()
    store = Store(_DB_PATH, objects_dir=_OBJECTS_DIR)
    try:
        for f in files:
            if f.suffix.lower() not in (".pdf", ".pptx"):
                continue
            try:
                result = ingest_file(store, f)
            except Exception:
                continue
            doc_id = result.document_id
            if doc_id is None:
                continue
            fam = version_family(store, doc_id)
            if not fam or len(fam.get("nodes", [])) <= 1:
                continue                      # unrelated single doc -> no tree
            root = fam["root"]
            if root in seen_families:
                continue                      # one tree per family
            seen_families.add(root)
            trees.append({
                "doc_id": str(doc_id),       # the file from THIS run (highlight it); string = 64-bit safe
                "name": f.name,
                "status": result.status,      # stored | duplicate | content_duplicate
                "family_size": len(fam["nodes"]),
                "source_type": fam["source_type"],
            })
        total_images = store.conn.execute("SELECT COUNT(*) FROM page_image").fetchone()[0]
        unique_images = store.conn.execute("SELECT COUNT(*) FROM image").fetchone()[0]
        images = {"total": total_images, "unique": unique_images}
    finally:
        store.close()
    return trees, images


def _tom_rapport(mode: str, trash: str | None) -> dict:
    """Rapporten när mappen saknar PDF/PPTX/bilder."""
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
        "version_trees": [], "images": {"total": 0, "unique": 0},
    }

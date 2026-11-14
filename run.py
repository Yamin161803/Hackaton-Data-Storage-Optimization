#!/usr/bin/env python3
"""Easy launcher for the project's apps. Works the same on Windows, macOS and Linux.

    python run.py                      menu: pick what to run
    python run.py setup                install the Python packages
    python run.py check                show what is installed and what is missing
    python run.py web                  start the website and open it in the browser
    python run.py notes <video|folder> make meeting notes (extra options go to src/app.py)
    python run.py dedupe <folder>      find duplicate slides/documents (report only)
    python run.py pdf <file.pdf>       PDF -> Markdown with shared images
    python run.py lookup <file>        video -> its notes, or notes -> its video
    python run.py test                 run the tests

See HOW_TO_RUN.md for details. Everything runs with the Python that runs this script,
so activate your virtual environment first if you use one.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
PY = sys.executable

MEDIA = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",      # video
         ".mp3", ".wav", ".m4a", ".flac", ".ogg"}              # audio only


def run(*args: str | Path) -> int:
    """Run a command and return its exit code."""
    cmd = [str(a) for a in args]
    print(">", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130


# ---------------------------------------------------------------- commands

def cmd_setup(_extra: list[str]) -> int:
    code = run(PY, "-m", "pip", "install", "-r", ROOT / "requirements.txt", "faster-whisper")
    if code == 0:
        print("\nDone. Optional extras (see HOW_TO_RUN.md):")
        print("  summaries/keywords: install Ollama, then  ollama pull llama3.2")
        print("  speaker labels:     pip install pyannote.audio torch soundfile  + .hf_token")
    return code


def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _ollama_running() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
            return True
    except OSError:
        return False


def _gpu() -> str:
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - not installed or no CUDA
        return "no (CPU will be used)"
    return f"yes ({n} CUDA device{'s' if n != 1 else ''})" if n else "no (CPU will be used)"


def cmd_check(_extra: list[str]) -> int:
    ok = "OK     "
    no = "MISSING"
    opt = "--     "
    rows = [
        (sys.version_info >= (3, 10), True, f"Python {sys.version.split()[0]} (3.10+ needed)"),
        (_has("numpy") and _has("flask") and _has("pypdf") and _has("pptx") and _has("PIL"),
         True, "packages from requirements.txt  ->  python run.py setup"),
        (_has("faster_whisper"), True, "faster-whisper (speech to text)  ->  python run.py setup"),
        (bool(shutil.which("ffmpeg")) or _has("imageio_ffmpeg"), True,
         "ffmpeg (system or imageio-ffmpeg)"),
        (_ollama_running(), False, "Ollama running on localhost:11434 (summary, keywords)"),
        (_has("pyannote.audio") or _has("pyannote"), False,
         "pyannote.audio (speaker labels, --speakers)"),
        ((ROOT / ".hf_token").is_file() or bool(os.environ.get("HF_TOKEN")),
         False, "Hugging Face token: .hf_token or HF_TOKEN (speaker labels)"),
    ]
    missing = 0
    for good, required, text in rows:
        mark = ok if good else (no if required else opt)
        missing += (not good and required)
        print(f"  [{mark}] {text}")
    print(f"  [info   ] GPU: {_gpu()}")
    print("\nRequired parts missing: run  python run.py setup" if missing
          else "\nReady. Rows marked -- are optional.")
    return 1 if missing else 0


def cmd_web(extra: list[str]) -> int:
    port = "8000"
    if "--port" in extra and extra.index("--port") + 1 < len(extra):
        port = extra[extra.index("--port") + 1]
    url = f"http://localhost:{port}"
    print(f"Starting the website at {url}  (stop with Ctrl+C)")
    print(f"  upload page: {url}/        duplicate cleanup: {url}/dedupe")
    threading.Timer(2.5, webbrowser.open, [url]).start()
    return run(PY, SRC / "website" / "server.py", *extra)


def _media_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.suffix.lower() in MEDIA and p.is_file())


def cmd_notes(extra: list[str]) -> int:
    if not extra or extra[0].startswith("-"):
        print("usage: python run.py notes <video, audio or folder> [app.py options]\n"
              "e.g.   python run.py notes data/raw/talk.mp4 --keywords\n"
              "       python run.py notes data/raw --no-summary")
        return 2
    target, options = Path(extra[0]), extra[1:]
    if not target.exists():
        print(f"error: {target} not found")
        return 1
    files = _media_in(target) if target.is_dir() else [target]
    if not files:
        print(f"no video or audio files in {target}")
        return 1
    failed = []
    for i, f in enumerate(files, start=1):
        if len(files) > 1:
            print(f"\n=== {i}/{len(files)}: {f.name} ===")
        if run(PY, SRC / "app.py", f.resolve(), *options) != 0:
            failed.append(f.name)
    if len(files) > 1:
        print(f"\n{len(files) - len(failed)} of {len(files)} done"
              + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def cmd_dedupe(extra: list[str]) -> int:
    if not extra or extra[0].startswith("-"):
        extra = [str(ROOT / "data" / "raw"), *extra]
        print("no folder given, using data/raw")
    return run(PY, SRC / "dedupe" / "slide_dedupe.py", *extra)


def cmd_pdf(extra: list[str]) -> int:
    if not extra:
        print("usage: python run.py pdf <file.pdf> [--out folder]")
        return 2
    return run(PY, SRC / "notes" / "pdf_to_markdown.py", *extra)


def cmd_lookup(extra: list[str]) -> int:
    if not extra:
        print("usage: python run.py lookup <video or notes.md> [--in folder]")
        return 2
    kind = "video" if Path(extra[0]).suffix.lower() == ".md" else "notes"
    return run(PY, SRC / "lookup.py", kind, *extra)


def cmd_test(extra: list[str]) -> int:
    return run(PY, "-m", "pytest", ROOT / "tests", "-q", *extra)


COMMANDS = {
    "setup": (cmd_setup, "install the Python packages"),
    "check": (cmd_check, "show what is installed and what is missing"),
    "web": (cmd_web, "start the website and open it in the browser"),
    "notes": (cmd_notes, "make meeting notes from a video/audio file or a folder"),
    "dedupe": (cmd_dedupe, "find duplicate slides/documents in a folder (report only)"),
    "pdf": (cmd_pdf, "PDF -> Markdown with shared images"),
    "lookup": (cmd_lookup, "find the notes for a video, or the video for notes"),
    "test": (cmd_test, "run the tests"),
}

# what the menu asks for after picking a command
_ASK = {
    "notes": "Video, audio file or folder (e.g. data/raw/talk.mp4): ",
    "dedupe": "Folder to check [data/raw]: ",
    "pdf": "PDF file: ",
    "lookup": "Video or notes .md file: ",
}


def menu() -> int:
    names = list(COMMANDS)
    print("What do you want to run?\n")
    for i, name in enumerate(names, start=1):
        print(f"  {i}. {name:<7} {COMMANDS[name][1]}")
    try:
        choice = input("\nNumber (Enter to quit): ").strip()
        if not choice:
            return 0
        if not choice.isdigit() or not 1 <= int(choice) <= len(names):
            print("not a valid choice")
            return 2
        name = names[int(choice) - 1]
        extra = []
        if name in _ASK:
            answer = input(_ASK[name]).strip().strip('"')
            if answer:
                extra.append(answer)
            elif name != "dedupe":
                print("nothing given")
                return 2
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    return COMMANDS[name][0](extra)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return menu()
    if argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[0] not in COMMANDS:
        print(f"unknown command {argv[0]!r}. Commands: {', '.join(COMMANDS)}")
        return 2
    return COMMANDS[argv[0]][0](argv[1:])


if __name__ == "__main__":
    sys.exit(main())

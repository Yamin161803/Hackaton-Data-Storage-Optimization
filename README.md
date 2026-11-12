# Hackaton-Data-Storage-Optimization

## Requirements

- Python 3.11
- System dependencies:
  - ffmpeg
  - tesseract-ocr
  - LibreOffice (headless)

You can use the provided `Dockerfile` for a consistent setup.

## Project layout

- `src/distill/` — main package and `distill` CLI
- `samples/` — fake source platform data (git-ignored)
  - `sharepoint/`
  - `onedrive_userA/`
- `out/` — generated outputs (git-ignored)
- `tests/fixtures/` — test fixture files
- `docs/` — project docs

## Quick start

```bash
make setup
make demo
make test
```
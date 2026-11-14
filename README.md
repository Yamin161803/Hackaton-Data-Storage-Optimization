# Hackaton-Data-Storage-Optimization

Cut down how much space meeting recordings and slide decks take up, without losing
what's in them.

- **Video/audio → meeting notes.** A recording becomes one Markdown file: the key
  frames (a new slide, a scene change), what was said while each was on screen, and
  optionally a summary, search keywords and speaker labels. Every notes file carries
  the recording's fingerprint, so you can check whether a video is already saved
  before deleting it.
- **Duplicate cleanup for slides and documents.** Every slide in PDF / PowerPoint /
  image files is hashed, and only the fewest files that still hold every unique slide
  are kept.
- **A local website** that does both from the browser.

Everything runs locally: speech to text uses faster-whisper (GPU if present, otherwise
CPU), and summaries and keywords come from a local model through Ollama.

## Quick start

```bash
python run.py setup     # install
python run.py check     # what is ready
python run.py           # menu, or: python run.py web | notes <video> | dedupe <folder>
```

See [HOW_TO_RUN.md](HOW_TO_RUN.md) for every command. The rest of this README explains
the tools themselves.

## Setup

Python 3.10+.

```bash
cd Hackaton-Data-Storage-Optimization
uv venv .venv --python 3.10          # or: python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt   # numpy, flask, imageio-ffmpeg, pypdf, python-pptx, pillow, pytest
uv pip install faster-whisper        # speech to text (not in requirements.txt)
```

ffmpeg: a system `ffmpeg` on `PATH` is used if there is one, otherwise the one bundled
with `imageio-ffmpeg`.

Optional extras:

| For | Install |
| --- | --- |
| Summary and keywords | [Ollama](https://ollama.com), then `ollama pull llama3.2` (the default model; change it with `--model`) |
| Speaker labels (`--speakers`) | `pip install pyannote.audio torch soundfile`, plus a Hugging Face token in `HF_TOKEN` or a `.hf_token` file in the project folder. The pyannote model is gated, so accept its terms once on huggingface.co. |
| GPU (CUDA) | `uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` |

### GPU

The device is detected automatically:

- GPU present → `cuda` + `float16`
- no GPU → `cpu` + `int8`

So the same code runs on a laptop and on a GPU box without changes. If a GPU is visible
but its CUDA libraries are missing, `app.py` falls back to the CPU. Check that the GPU
is found:

```bash
python -c "import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"
```

On an RTX 3070 (8 GB), `large-v3` in `float16` fits (~5 GB) and runs at roughly
10–15x real time. `--batched` adds another 3–4x on many or long files.

## Video → meeting notes (one command)

```bash
python src/app.py "data/raw/talk.mp4"                     # notes + frames + summary
python src/app.py "data/raw/talk.mp4" --no-summary        # without the AI summary
python src/app.py "data/raw/talk.mp4" --keywords          # + AI search keywords
python src/app.py "data/raw/talk.mp4" --speakers          # + who is speaking
python src/app.py meeting.mp4 --num-speakers 3            # exact speaker count, if known
python src/app.py talk.mp4 --whisper-model large-v3 --lang en
python src/app.py talk.mp4 --hint "Talk about GraalVM native image"
```

Steps: hash the file, pick key frames and extract the audio, transcribe, then write
the notes.

What is kept:

- `data/meeting_notes/<video>.md`: the notes (change with `-o`)
- `data/resources/<video>/frame_*.jpg`: the frames, linked from the notes with relative
  paths (change with `--resources`). Use `--embed-images` to put the frames inside the
  `.md` instead, as one self-contained file.

The audio and transcript are made in a temporary folder and deleted afterwards.
Audio-only files (e.g. an MP3) get a part every 5 minutes instead of frames. If Ollama
or speaker detection fails, the notes are still written without that part.

**Key frames.** The first frame is always kept; after that a frame is kept when at
least `--threshold` (default 0.05 = 5 %) of the picture changed since the last kept
frame. `--mode auto` (default) detects camera footage (the picture changes almost every
frame) and then uses `--camera-threshold` (default 0.40), so only real scene changes are
kept. Force it with `--mode presentation` or `--mode camera`.

**Silent frames.** After that, frames shown while nobody speaks are filtered. A frame
counts as silent when no text ends up under it, or when it has fewer than `--min-words`
(default 10) words and is on screen under 10 s, e.g. the presenter moving windows
around. A short pause (up to `--max-silent-frames`, default 3, frames in a row) is
kept; a longer run is left out of the notes and not saved, and its text moves to the
previous kept frame, so nothing said is lost. `--keep-silent-frames` turns this off.
Videos without speech keep all their frames.

Other options: `--whisper-model` (`tiny|base|small|medium|large-v3|distil-large-v3`,
default `base`), `--device auto|cuda|cpu`, `--batched`, `--model` (Ollama model),
`--ollama-url`. Run `python src/app.py -h` for the full list.

### Fingerprint and lookup

Every notes file starts with the file's fingerprint (`src/videohash.py`), so the video
can be found from the notes and the notes from the video, even after renaming:

```yaml
---
file_size: 220480662                                  # bytes
quick_hash: 5cfa317d65a6c076...                       # reads only 2 MiB
quick_hash_method: sha256-size-first-last-1MiB
full_hash: 56788476f81d23da...                        # whole file
full_hash_algorithm: sha256
keywords: ["GraalVM", "native image", ...]            # with --keywords
speakers: 2                                           # with --speakers
---
```

Find one from the other. Size is checked first (free), then the quick hash, and the
full hash only to confirm:

```bash
python src/lookup.py notes "data/raw/talk.mp4"             # video -> its notes
python src/lookup.py video "data/meeting_notes/talk.md"    # notes -> its video
python src/lookup.py video notes.md --in D:/archive        # search another folder
```

A renamed copy is found; a re-encoded or edited video is not, because its bytes differ.

## Local website

```bash
python src/website/server.py          # or: cd src && python -m website
# then open http://localhost:8000
```

**Upload page** (`/`): drop videos or a whole folder, choose summary, keywords and
speaker labels per upload, and download the notes (one `.md` with the frames inside).
The server also saves them in `data/meeting_notes` / `data/resources`. A file that was
already processed (same fingerprint) is returned straight away; if the saved notes lack
something you asked for, the video is processed again.

**Rensa dubbletter** (`/dedupe`): runs `slide_dedupe` on a folder you pick. It shows
the run time and how much space removing the rejected files would free. Optionally it
moves them to a trash folder (default `data/trash`), and, after you confirm, empties
that folder automatically or on demand. Nothing is deleted if the folder changed since
you confirmed. Every web run uses a fresh index, so the whole folder is analysed each
time.

Server options: `--port` (default 8000, or `$PORT`), `--whisper-model`, and the
defaults `--no-summary`, `--keywords`, `--speakers`.

## Slides and documents

```bash
# Which PDF / PowerPoint / image files can go without losing a unique slide?
python src/dedupe/slide_dedupe.py data/raw                    # report only
python src/dedupe/slide_dedupe.py data/raw --mode batch --trash data/trash

# PDF -> Markdown, each image saved once and named by its hash
python src/notes/pdf_to_markdown.py "data/raw/deck.pdf" --out data/docs
```

`slide_dedupe` hashes every slide from its normalized text and the hashes of its images
(an image file counts as one slide; page numbers are ignored). Byte-identical files are
found first by size, quick hash and full hash, and skip slide extraction. Then it keeps
the fewest files that still hold every unique slide:

- `--mode online` (default): files are handled one at a time; state is kept in
  `data/slide_index.json` (`--index`), so later runs only process new files.
- `--mode batch`: all files at once with greedy set cover; usually a smaller result.

Files are only moved when `--trash` is given. Other options: `--workers N`,
`--text-only`, `--keep-page-numbers`, `--fast-images`, `--no-prefilter`,
`--report-json`, `--profile`, `--quiet`. Run with `-h` for details.

## The steps on their own

Run these from `src/`:

```bash
cd src

# Key frames + audio -> <video>_split/ (frames/, timestamps.csv, audio.wav)
python -m video_splitter talk.mp4 --threshold 0.05 --audio-format mp3

# Speech to text: one .txt per input with "[start -> end] text" lines
python -m transcription.transcribe meeting.mp4 --model base        # quick check
python -m transcription.transcribe ./recordings --model large-v3 --batched
python -m transcription.transcribe meeting.mp4 --lang en           # skip language detection

# Transcript with speaker labels ("[start -> end] SPEAKER_00: text")
python -m transcription.diarize meeting.wav --model large-v3 --num-speakers 3

# Split folder + transcript -> Markdown notes
python -m notes.meeting_notes ../data/talk_split -o talk.md --title "My talk"
```

`transcribe` prints a real-time factor (e.g. `12.3x real-time`) so you can compare CPU
and GPU throughput. `distil-large-v3` is a faster, English-only alternative to
`large-v3`.

## Tests

```bash
python -m pytest tests
```

Speech recognition and Ollama are replaced by fakes, so the tests run fast and offline.

## Project layout

```text
src/
  app.py              video/audio -> meeting notes (one command)
  lookup.py           find the notes for a recording, and the reverse
  videohash.py        file fingerprint: size, quick hash, full hash
  video_splitter/     key frames + audio (presentation / camera detection)
  transcription/      transcribe.py (faster-whisper), diarize.py (pyannote speaker labels)
  notes/              meeting_notes.py (Markdown), summarize.py (Ollama), pdf_to_markdown.py
  dedupe/             slide_dedupe.py (duplicate slide/document cleanup)
  store/              document store: page-level dedup, versions, shared images, search
  website/            Flask server, dedupe_api.py, static pages
tests/                pytest tests for all of the above
data/                 raw/, meeting_notes/, resources/, trash/ (contents git-ignored),
                      slide_index.json, database
docs/                 notes.txt (design notes), ericsson.pdf (challenge brief)
```

## Document store (PDFs & slide decks) — dedup + versioning + search

`src/store/` stores PDFs and slide decks so that hundreds of thousands of files
stay searchable and sorted, with heavy deduplication and version tracking.

How it works (see the schema in `src/store/schema.sql`):

- **Exact-duplicate gate.** A document's full-file hash (same fingerprint idea as
  `src/videohash.py`: size -> quick hash -> full hash) is checked first; an exact
  duplicate is discarded on ingest.
- **Page-level dedup.** Each page/slide is content-addressed by a hash of its
  normalized text + referenced image hashes. A document is stored as an *ordered
  list of page hashes* (a manifest), never as page content — so a 50-slide deck
  that shares 48 pages with another only adds 2 new pages.
- **No overlap threshold.** Shared title/closing slides dedup individually and
  never force a false "these are the same deck" merge. Identity is *content*,
  never the filename.
- **Inverted index** (`page_hash -> document`) makes "which documents share this
  page?" fast, so version detection only diffs candidates instead of scanning
  every document.
- **Version lineage** is recorded in `document_edge` (extends / shortened /
  reordered / modified). Shortening a deck and re-adding it is kept (not deleted),
  costs near-zero extra storage, and is recorded as a *shortened* (subset) version.

Extractors (e.g. the PDF/PPTX handlers) produce `store.Page` objects and hand a
`store.DocumentInput` to `Store.ingest(...)`.

Search and inspect:

```bash
# keyword search over page/slide text (FTS5), sortable/filterable
python src/store/search.py find "budget forecast"
python src/store/search.py find "roadmap" --type pdf --sort date --limit 20

# list documents, sorted by date/size/pages/name
python src/store/search.py docs --sort size
python src/store/search.py docs --name "*review*"

# show a document's version relationships
python src/store/search.py versions <document_id>
```

Tests: `pytest tests/test_store.py` (exact-dup discard, shared start/end slides
without false merge, shorten-and-re-add, extend, content-not-filename identity).

### Slide pictures vs speaker notes (PPTX) and shared image store

- **PPTX** pages carry two sub-identities: `image_hash` (the slide picture) and
  `text_hash` (the speaker notes). "Same picture, rewritten notes" is reported as
  a *visual duplicate* (`IngestResult.visual_duplicates`) — a reworded slide,
  not an unrelated new page. **PDF** keeps a combined text+image identity.
- **Shared image object store:** pictures from PDFs *and* decks are stored once,
  content-addressed and sharded on disk under `objects/ab/cd/<hash>`. If 100
  documents use the same logo, the bytes live on disk once; `page_image` records
  which pages reference it (so nothing is deleted while referenced). A document
  can be rebuilt from its text plus the single stored copy of each picture:

  ```python
  from store import Store, DocumentInput, Page, SOURCE_PDF
  s = Store("database")
  img = s.put_image(open("logo.png", "rb").read())   # stored once; returns hash
  page = Page(text="page text", image_hashes=[img])
  s.ingest(DocumentInput(full_hash=..., file_size=..., source_type=SOURCE_PDF,
                         pages=[page], source_name="report.pdf"))
  s.link_page_images(page.hash, [img])
  s.reconstruct(doc_id)   # -> [{position, text, images:[paths]}] to remake the doc
  ```

## Notes

- `.venv/`, model caches, `data/` contents and `.hf_token` are git-ignored.
- Whisper models download automatically on first use (cached under
  `~/.cache/huggingface`).

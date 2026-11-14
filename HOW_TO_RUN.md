# How to run

Every app in this project starts through one launcher, `run.py`. It works the same on
Windows, macOS and Linux. Run it from the project folder:

```bash
python run.py
```

With no arguments it shows a menu: pick a number and answer the question. Every menu
item is also a command, listed below. `python run.py -h` lists them all.

| Command | What it does |
| --- | --- |
| `python run.py setup` | install the Python packages (`--speakers` adds speaker labels) |
| `python run.py check` | show what is installed and what is missing |
| `python run.py web` | start the website and open it in the browser |
| `python run.py notes <video or folder>` | make meeting notes |
| `python run.py dedupe <folder>` | find duplicate slides and documents (report only) |
| `python run.py pdf <file.pdf>` | PDF to Markdown, each image saved once |
| `python run.py lookup <file>` | find the notes for a video, or the video for notes |
| `python run.py test` | run the tests |

## 1. First time: install

You need Python 3.10 or newer. A virtual environment is optional but recommended:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

python run.py setup             # pip install -r requirements.txt
python run.py check             # shows what is ready and what is missing
```

`run.py` always uses the Python it was started with, so activate the virtual
environment before every session.

Example `check` output on a machine with nothing optional installed:

```text
  [OK     ] Python 3.11.9 (3.10+ needed)
  [OK     ] packages from requirements.txt  ->  python run.py setup
  [OK     ] test packages (pytest, pymupdf)  ->  python run.py setup
  [OK     ] faster-whisper (speech to text)  ->  python run.py setup
  [OK     ] ffmpeg (system or imageio-ffmpeg)
  [--     ] Ollama running on localhost:11434 (summary, keywords)
  [--     ] pyannote.audio (speaker labels, --speakers)
  [--     ] Hugging Face token: .hf_token or HF_TOKEN (speaker labels)
  [info   ] GPU for transcription: no (CPU will be used)

Ready. Rows marked -- are optional.
```

Rows marked `MISSING` are required. Rows marked `--` are optional:

| Optional part | What it adds | How to get it |
| --- | --- | --- |
| Ollama | summary and search keywords in the notes | install from [ollama.com](https://ollama.com), then `ollama pull llama3.2` |
| pyannote.audio + token | "who is speaking" labels | `python run.py setup --speakers`, then put a Hugging Face token in a `.hf_token` file in the project folder. Accept the terms of `pyannote/speaker-diarization-community-1` on huggingface.co once with that account. |
| NVIDIA GPU | 10x or more faster transcription | `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`; used automatically when present |

Without the optional parts everything still works: the notes are just written without
a summary, keywords or speaker labels.

The Python packages are listed in two files:

- `requirements.txt`: everything needed to run the project and its tests.
- `requirements-speakers.txt`: the optional speaker labels (pyannote.audio, torch,
  soundfile).

The first transcription downloads the whisper model (`base` is about 150 MB), and the
first `--speakers` run downloads the pyannote model. Both are cached afterwards.

### GPU for speaker labels

Transcription uses the GPU on its own, but speaker labels (pyannote) need the CUDA
build of PyTorch. `python run.py check` tells you which one you have. To switch:

```bash
pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu126
```

This is a ~2.5 GB download. Afterwards `check` shows "speaker labels run on: GPU".
If the GPU runs out of memory, speaker detection falls back to the CPU by itself.

## 2. The website (easiest)

```bash
python run.py web
python run.py web --port 8080 --whisper-model large-v3
```

This starts the server and opens <http://localhost:8000> in the browser. Stop it with
Ctrl+C.

- **Video notes** (`/`): drop videos or a folder, tick summary / keywords / speakers,
  press start, and download the notes. A video that was already processed is returned
  straight away.
- **Remove duplicates** (`/dedupe`): pick a folder (e.g. `data/raw`) and see which
  PDF / PowerPoint / image files can go without losing a unique slide, and how much
  space that frees. The PDFs and decks are also added to the document store, and new
  versions of a document show their version tree. *Clear the version store* empties it.
- **Versions** (`/versions`): every version family in the store as a graph, with the
  space saved. Download any version, restored from the store.

The pages are in English. Click **SV** in the header for Swedish (**EN** switches back);
the browser remembers the choice.

Extra options are passed to the server: `--port`, `--whisper-model`, `--no-summary`,
`--keywords`, `--speakers`.

## 3. Meeting notes from the command line

```bash
python run.py notes data/raw/talk.mp4                  # one video
python run.py notes data/raw                           # every video/audio file in a folder
python run.py notes data/raw/talk.mp4 --keywords --speakers
python run.py notes data/raw/talk.mp4 --whisper-model large-v3 --lang en
```

The notes go to `data/meeting_notes/<name>.md` and the frames to
`data/resources/<name>/`. Audio files (mp3, wav, m4a, ...) work too. Put your own
recordings in `data/raw/`; that folder is not in git.

Useful options (all from `src/app.py`; see `python src/app.py -h`):

| Option | What it does |
| --- | --- |
| `--no-summary` | skip the Ollama summary (faster) |
| `--keywords` | add search keywords |
| `--speakers` / `--num-speakers 3` | label who is speaking |
| `--whisper-model large-v3` | better transcription (default `base` is fast) |
| `--lang en` | skip language detection |
| `--hint "GraalVM native image"` | context for the summary and keywords, e.g. how names are spelled |
| `--max-silent-frames 3` | leave out longer runs of frames where nobody speaks |
| `--min-words 10` | a frame shown under 10 s with fewer words counts as silent |
| `--keep-silent-frames` | keep every frame |
| `--embed-images` | put the frames inside the `.md` (one self-contained file) |
| `-o notes.md`, `--resources folder` | write the notes and frames somewhere else |

## 4. Duplicate slides and documents

```bash
python run.py dedupe                                    # checks data/raw, report only
python run.py dedupe D:/shared/presentations
python run.py dedupe data/raw --mode batch --trash data/trash   # move the duplicates
```

Without `--trash` nothing is moved. The tool remembers what it has seen in
`data/slide_index.json`; add `--index some/other.json` for a separate test run.

## 5. Document store and search

The website's **Remove duplicates** page fills the store. You can also add files and
search them from the command line:

```bash
cd src
python -m store.ingest_files ../data/database ../data/raw/*.pdf  # add files
python store/search.py find "orchestration"                       # full-text search
python store/search.py docs --sort size                           # list documents
python store/search.py versions <document_id>                     # version relations
```

## 6. Other tools

```bash
python run.py pdf data/raw/deck.pdf --out data/docs     # PDF -> Markdown, images saved once
python run.py lookup data/raw/talk.mp4                  # video -> its notes
python run.py lookup data/meeting_notes/talk.md         # notes -> its video
python run.py lookup notes.md --in D:/archive           # look for the video elsewhere
python run.py test                                      # run all tests (offline, ~30 s)
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `No module named ...` | `python run.py setup` (in the activated virtual environment) |
| `check` says `MISSING` | `python run.py setup`, then `check` again |
| Tests are skipped | a test package is missing: `python run.py setup` |
| No summary or keywords in the notes | start Ollama (`ollama serve`) and `ollama pull llama3.2` |
| "GPU failed ... falling back to CPU" | CUDA libraries missing; see the GPU row above, or ignore it (CPU works) |
| No speaker labels | `python run.py setup --speakers`, add `.hf_token`, check with `python run.py check` |
| "could not load pyannote/speaker-diarization-community-1" | the token is wrong, or the model's terms are not accepted on huggingface.co |
| Port 8000 already in use | `python run.py web --port 8080` |
| The first run is slow | whisper downloads the model once (cached under `~/.cache/huggingface`) |
| Odd characters in the console on Windows | run through `python run.py ...` (it switches the output to UTF-8); the files themselves are always UTF-8 |

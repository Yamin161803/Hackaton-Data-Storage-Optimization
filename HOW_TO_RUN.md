# How to run

Every app in this project starts through one launcher, `run.py`. It works the same on
Windows, macOS and Linux. Run it from the project folder:

```bash
python run.py
```

With no arguments it shows a menu: pick a number and answer the question. Every menu
item is also a command, listed below.

## 1. First time: install

You need Python 3.10 or newer. A virtual environment is optional but recommended:

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

python run.py setup             # installs requirements.txt + faster-whisper
python run.py check             # shows what is ready and what is missing
```

`run.py` always uses the Python it was started with, so activate the virtual
environment before every session.

Example `check` output:

```text
  [OK     ] Python 3.11.9 (3.10+ needed)
  [OK     ] packages from requirements.txt  ->  python run.py setup
  [OK     ] faster-whisper (speech to text)  ->  python run.py setup
  [OK     ] ffmpeg (system or imageio-ffmpeg)
  [--     ] Ollama running on localhost:11434 (summary, keywords)
  [--     ] pyannote.audio (speaker labels, --speakers)
  [--     ] Hugging Face token: .hf_token or HF_TOKEN (speaker labels)
  [info   ] GPU: no (CPU will be used)
```

Rows marked `MISSING` are required. Rows marked `--` are optional:

| Optional part | What it adds | How to get it |
| --- | --- | --- |
| Ollama | summary and search keywords in the notes | install from [ollama.com](https://ollama.com), then `ollama pull llama3.2` |
| pyannote.audio + token | "who is speaking" labels | `pip install pyannote.audio torch soundfile`, then put a Hugging Face token in a `.hf_token` file in the project folder (accept the pyannote model's terms on huggingface.co once) |
| NVIDIA GPU | 10x or more faster transcription | `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`; used automatically when present |

Without the optional parts everything still works: the notes are just written without
a summary, keywords or speaker labels.

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

- **Upload page** (`/`): drop videos or a folder, tick summary / keywords / speakers,
  press start, and download the notes. A video that was already processed is returned
  straight away.
- **Remove duplicates** (`/dedupe`): pick a folder and see which PDF / PowerPoint /
  image files can go without losing a unique slide, and how much space that frees.

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
`data/resources/<name>/`. Audio files (mp3, wav, m4a, ...) work too.

Useful options (all from `src/app.py`; see `python src/app.py -h`):

| Option | What it does |
| --- | --- |
| `--no-summary` | skip the Ollama summary (faster) |
| `--keywords` | add search keywords |
| `--speakers` / `--num-speakers 3` | label who is speaking |
| `--whisper-model large-v3` | better transcription (default `base` is fast) |
| `--lang en` | skip language detection |
| `--max-silent-frames 3` | leave out longer runs of frames where nobody speaks |
| `--min-words 10` | a frame shown under 10 s with fewer words counts as silent |
| `--keep-silent-frames` | keep every frame |
| `--embed-images` | put the frames inside the `.md` (one self-contained file) |

## 4. Duplicate slides and documents

```bash
python run.py dedupe                                    # checks data/raw, report only
python run.py dedupe D:/shared/presentations
python run.py dedupe data/raw --mode batch --trash data/trash   # move the duplicates
```

Without `--trash` nothing is moved. The tool remembers what it has seen in
`data/slide_index.json`; add `--index some/other.json` for a separate test run.

## 5. Other tools

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
| No summary or keywords in the notes | start Ollama (`ollama serve`) and `ollama pull llama3.2` |
| "GPU failed ... falling back to CPU" | CUDA libraries missing; see the GPU row above, or ignore it (CPU works) |
| No speaker labels | install pyannote.audio and add `.hf_token`; check with `python run.py check` |
| Port 8000 already in use | `python run.py web --port 8080` |
| The first run is slow | whisper downloads the model once (cached under `~/.cache/huggingface`) |

# Hackaton-Data-Storage-Optimization

Speech-to-text for meeting video recordings using **faster-whisper**.
Goal: compress a large volume of videos into text transcripts, prioritizing
**speed + accuracy**.

The transcription script (`src/transcription/transcribe.py`) **auto-detects the device**:
- GPU present  → `cuda` + `float16` (production, e.g. RTX 3070)
- No GPU       → `cpu` + `int8` (laptop testing — current setup)

So the same code runs on the laptops now and on the GPU box later with no edits.

## Setup (laptops, CPU — current)

```bash
cd Hackaton-Data-Storage-Optimization
uv venv .venv --python 3.10
source .venv/bin/activate
uv pip install faster-whisper
```

Optional (for video formats PyAV can't decode directly):
```bash
sudo apt update && sudo apt install -y ffmpeg
```

## Setup (GPU box, CUDA — later, RTX 3070)

Same as above, plus the NVIDIA CUDA 12 + cuDNN runtime libraries that
CTranslate2 (faster-whisper's backend) needs. On Linux the simplest path is to
let pip pull them in:

```bash
uv pip install faster-whisper
uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Then confirm the GPU is visible:
```bash
python -c "import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"
```
A number > 0 means the script will automatically use `device=cuda, float16`.

On the 3070 (8 GB VRAM), `large-v3` in `float16` fits (~5 GB) and runs at
roughly 10–15x real-time. Use `--batched` for a further 3–4x on many/long files.

## Video -> meeting notes (one command)

```bash
python src/app.py "data/raw/talk.mp4"                 # notes + frames + summary
python src/app.py "data/raw/talk.mp4" --no-summary    # without the AI summary
python src/app.py "data/raw/talk.mp4" --keywords      # + AI search keywords
python src/app.py "data/raw/talk.mp4" --speakers      # + who is speaking
```

Writes `data/meeting_notes/<video>.md` (each key frame with what was said, optional
summary) and the frames to `data/resources/<video>/`. Audio-only files (e.g. an MP3)
get a part every 5 minutes instead of frames. Summary and keywords come from a local
model through Ollama. Every notes file starts with the file's fingerprint
(`src/videohash.py`), so the video can be found from the notes and the notes from the
video, even after renaming:

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

Find one from the other (size first, then quick hash, full hash to confirm):

```bash
cd src
python lookup.py notes "../data/raw/talk.mp4"              # video -> its notes
python lookup.py video "../data/meeting_notes/talk.md"     # notes -> its video
python lookup.py video notes.md --in D:/archive            # search another folder
```

## Local website

```bash
pip install flask
python src/website/server.py          # then open http://localhost:8000
```

Drop videos on the page and download their notes (one `.md` with the frames inside).
The server also saves them in `data/meeting_notes` / `data/resources`. A file that was
already processed (same fingerprint) is returned straight away. On the page you choose
summary, search keywords and speaker labels per upload. Server options:
`--whisper-model large-v3`, `--port`, and defaults `--no-summary`, `--keywords`,
`--speakers`.

## Transcription only

```bash
source .venv/bin/activate
cd src

# Quick sanity check on a laptop (seconds):
python -m transcription.transcribe meeting.mp4 --model base

# Full quality (auto GPU if present, else CPU):
python -m transcription.transcribe meeting.mp4 --model large-v3

# Whole folder of recordings -> a .txt next to each:
python -m transcription.transcribe ./recordings --model large-v3

# Batched (big GPU speedup for high volume):
python -m transcription.transcribe ./recordings --model large-v3 --batched

# Force a language to skip auto-detect (en, sv, ...):
python -m transcription.transcribe meeting.mp4 --model large-v3 --lang en
```

Output: one `.txt` per input, with `[start -> end]` timestamps per segment.
The run prints a real-time factor (e.g. `12.3x real-time`) so you can compare
laptop-CPU vs GPU throughput.

## Notes
- `.venv/` and model cache are git-ignored.
- Models download automatically on first use (cached under `~/.cache/huggingface`).
- `distil-large-v3` is a faster, English-optimized alternative to `large-v3`
  if throughput matters more than multilingual coverage.

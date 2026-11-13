# Hackaton-Data-Storage-Optimization

Speech-to-text for meeting video recordings using **faster-whisper**.
Goal: compress a large volume of videos into text transcripts, prioritizing
**speed + accuracy**.

The transcription script (`transcribe.py`) **auto-detects the device**:
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

## Usage

```bash
source .venv/bin/activate

# Quick sanity check on a laptop (seconds):
python transcribe.py meeting.mp4 --model base

# Full quality (auto GPU if present, else CPU):
python transcribe.py meeting.mp4 --model large-v3

# Whole folder of recordings -> a .txt next to each:
python transcribe.py ./recordings --model large-v3

# Batched (big GPU speedup for high volume):
python transcribe.py ./recordings --model large-v3 --batched

# Force a language to skip auto-detect (en, sv, ...):
python transcribe.py meeting.mp4 --model large-v3 --lang en
```

Output: one `.txt` per input, with `[start -> end]` timestamps per segment.
The run prints a real-time factor (e.g. `12.3x real-time`) so you can compare
laptop-CPU vs GPU throughput.

## Notes
- `.venv/` and model cache are git-ignored.
- Models download automatically on first use (cached under `~/.cache/huggingface`).
- `distil-large-v3` is a faster, English-optimized alternative to `large-v3`
  if throughput matters more than multilingual coverage.

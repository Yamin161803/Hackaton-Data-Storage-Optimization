#!/usr/bin/env python3
"""Transcribe an audio/video file (or folder) to text using faster-whisper.

Device is auto-detected:
  - CUDA GPU present  -> device=cuda, compute_type=float16  (production, e.g. RTX 3070)
  - No GPU            -> device=cpu,  compute_type=int8     (laptop testing)

You can override with --device / --compute-type.

Examples:
    # Single file, quick sanity check on a laptop
    python transcribe.py meeting.mp4 --model base

    # Single file, full quality (auto picks GPU if available)
    python transcribe.py meeting.mp4 --model large-v3

    # Whole folder of videos -> .txt next to each, batched on GPU
    python transcribe.py ./recordings --model large-v3 --batched
"""
import argparse
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

try:
    from faster_whisper import BatchedInferencePipeline
    HAVE_BATCHED = True
except ImportError:
    HAVE_BATCHED = False

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4a", ".mp3", ".wav", ".flac", ".ogg"}


def detect_device(requested: str):
    """Return (device, compute_type). 'auto' picks cuda+float16 if a GPU is usable."""
    if requested != "auto":
        return requested, ("float16" if requested == "cuda" else "int8")
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def transcribe_one(model, batched_model, in_path: Path, lang, use_batched: bool):
    out_path = in_path.with_suffix(".txt")
    print(f"\n--- {in_path.name} ---")
    t1 = time.time()
    if use_batched and batched_model is not None:
        segments, info = batched_model.transcribe(
            str(in_path), language=lang, batch_size=16, vad_filter=True
        )
    else:
        segments, info = model.transcribe(
            str(in_path), language=lang, vad_filter=True
        )
    print(f"lang={info.language} (p={info.language_probability:.2f}) "
          f"duration={info.duration:.1f}s")

    lines = []
    for seg in segments:
        lines.append(f"[{seg.start:7.2f} -> {seg.end:7.2f}] {seg.text.strip()}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elapsed = time.time() - t1
    rtf = (info.duration / elapsed) if elapsed > 0 else 0
    print(f"done in {elapsed:.1f}s ({rtf:.1f}x real-time) -> {out_path}")
    return out_path


def main():
    p = argparse.ArgumentParser(description="faster-whisper transcription (CPU/GPU auto)")
    p.add_argument("input", help="Audio/video file OR a folder of them")
    p.add_argument("--model", default="base",
                   help="tiny|base|small|medium|large-v3|distil-large-v3 (default: base)")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"],
                   help="auto (default) picks GPU if present, else CPU")
    p.add_argument("--compute-type", default=None,
                   help="Override compute type (e.g. float16, int8, int8_float16)")
    p.add_argument("--lang", default=None, help="Force language code (en, sv). Default: auto")
    p.add_argument("--batched", action="store_true",
                   help="Use BatchedInferencePipeline (big GPU speedup for many/long files)")
    args = p.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERROR: not found: {in_path}", file=sys.stderr)
        sys.exit(1)

    device, compute_type = detect_device(args.device)
    if args.compute_type:
        compute_type = args.compute_type

    print(f"Model '{args.model}' on device={device} compute_type={compute_type}")
    if args.batched and not HAVE_BATCHED:
        print("WARNING: BatchedInferencePipeline not available in this faster-whisper "
              "version; falling back to sequential.")
    t0 = time.time()
    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    batched_model = None
    if args.batched and HAVE_BATCHED:
        batched_model = BatchedInferencePipeline(model=model)
    print(f"Model loaded in {time.time() - t0:.1f}s")

    if in_path.is_dir():
        files = sorted(f for f in in_path.iterdir()
                       if f.is_file() and f.suffix.lower() in MEDIA_EXTS)
        if not files:
            print(f"No media files in {in_path}", file=sys.stderr)
            sys.exit(1)
        print(f"Found {len(files)} media file(s) in {in_path}")
        for f in files:
            transcribe_one(model, batched_model, f, args.lang, args.batched)
    else:
        transcribe_one(model, batched_model, in_path, args.lang, args.batched)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Transcribe + diarize: produce a transcript with speaker labels.

Pipeline:
  1. faster-whisper -> text segments with [start, end] timestamps
  2. pyannote.audio -> speaker turns (who spoke when)
  3. merge: each transcript segment gets the speaker who overlaps it most

Device auto-detected (CUDA float16 if a GPU is present, else CPU int8) for
faster-whisper; pyannote uses CUDA if torch sees a GPU, else CPU.

HF token is read from the .hf_token file in the project root (or HF_TOKEN env var).

Usage:
    python -m transcription.diarize jre_clip.wav --model large-v3
    python -m transcription.diarize meeting.wav --model large-v3 --num-speakers 3
"""
import argparse
import os
import sys
import time
from pathlib import Path

import torch
import numpy as np
import soundfile as sf
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline

HERE = Path(__file__).resolve().parents[2]  # project root
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"


def read_token():
    tok = os.environ.get("HF_TOKEN")
    if tok:
        return tok.strip()
    tok_file = HERE / ".hf_token"
    if tok_file.exists():
        return tok_file.read_text().strip()
    sys.exit("ERROR: no HF token (set HF_TOKEN or create .hf_token)")


def fw_device():
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def assign_speaker(seg_start, seg_end, turns):
    """Pick the speaker with the most temporal overlap for [seg_start, seg_end]."""
    best, best_ov = "UNKNOWN", 0.0
    for t_start, t_end, spk in turns:
        ov = min(seg_end, t_end) - max(seg_start, t_start)
        if ov > best_ov:
            best_ov, best = ov, spk
    return best


def main():
    p = argparse.ArgumentParser(description="faster-whisper + pyannote diarization")
    p.add_argument("input", help="Audio file (wav recommended)")
    p.add_argument("--model", default="base", help="faster-whisper model size")
    p.add_argument("--lang", default=None, help="Force language code (en, sv)")
    p.add_argument("--num-speakers", type=int, default=None,
                   help="Exact number of speakers, if known (improves accuracy)")
    p.add_argument("--out", default=None, help="Output .txt (default: <input>.diarized.txt)")
    args = p.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        sys.exit(f"ERROR: not found: {in_path}")
    out_path = Path(args.out) if args.out else in_path.with_suffix(".diarized.txt")

    token = read_token()

    # --- 1. transcription ---
    dev, ctype = fw_device()
    print(f"[1/3] faster-whisper '{args.model}' on {dev}/{ctype}")
    t0 = time.time()
    wmodel = WhisperModel(args.model, device=dev, compute_type=ctype)
    segments, info = wmodel.transcribe(str(in_path), language=args.lang, vad_filter=True)
    segs = [(s.start, s.end, s.text.strip()) for s in segments]
    print(f"      {len(segs)} segments, lang={info.language}, "
          f"audio={info.duration:.0f}s, took {time.time()-t0:.0f}s")

    # --- 2. diarization ---
    print(f"[2/3] pyannote diarization ({DIARIZATION_MODEL})")
    t1 = time.time()
    dia = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token)
    if torch.cuda.is_available():
        dia.to(torch.device("cuda"))
    # Load audio in-memory via soundfile (avoids torchcodec / CUDA-lib issues).
    data, sr = sf.read(str(in_path), dtype="float32", always_2d=True)  # (time, ch)
    waveform = torch.from_numpy(data.T.copy())  # -> (ch, time)
    dia_kwargs = {}
    if args.num_speakers:
        dia_kwargs["num_speakers"] = args.num_speakers
    diarization = dia({"waveform": waveform, "sample_rate": sr}, **dia_kwargs)
    # pyannote v4 returns a DiarizeOutput; the Annotation is .speaker_diarization
    annotation = getattr(diarization, "speaker_diarization", diarization)
    turns = [(seg.start, seg.end, spk)
             for seg, _, spk in annotation.itertracks(yield_label=True)]
    speakers = sorted(set(t[2] for t in turns))
    print(f"      {len(turns)} turns, {len(speakers)} speakers, took {time.time()-t1:.0f}s")

    # --- 3. merge ---
    print("[3/3] merging speaker labels into transcript")
    lines = []
    for start, end, text in segs:
        spk = assign_speaker(start, end, turns)
        lines.append(f"[{start:7.2f} -> {end:7.2f}] {spk}: {text}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Done -> {out_path}")


if __name__ == "__main__":
    main()

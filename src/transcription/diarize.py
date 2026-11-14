#!/usr/bin/env python3
"""Transcribe + diarize: produce a transcript with speaker labels.

Pipeline:
  1. faster-whisper -> text segments with [start, end] timestamps
  2. pyannote.audio -> speaker turns (who spoke when)
  3. merge: each transcript segment gets the speaker who overlaps it most

Device auto-detected (CUDA float16 if a GPU is present, else CPU int8) for
faster-whisper; pyannote uses CUDA if PyTorch can use the GPU, else the CPU (and says
so when a GPU is present but PyTorch is a CPU-only build). A failed GPU run (e.g. out
of memory) is retried on the CPU.

HF token is read from the .hf_token file in the project root (or HF_TOKEN env var).
The pyannote model is gated: accept its terms once on huggingface.co with that account.

Output lines: "[  12.34 ->   18.90] SPEAKER_00: text" (notes.meeting_notes reads these
with --speakers).

Usage:
    python -m transcription.diarize jre_clip.wav --model large-v3
    python -m transcription.diarize meeting.wav --model large-v3 --num-speakers 3

From other code:
    turns = diarize_turns("audio.wav", read_token())   # [(start, end, "SPEAKER_00"), ...]
    assign_speaker(seg_start, seg_end, turns)          # -> "SPEAKER_00"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable

# torch, soundfile, pyannote and faster-whisper are imported inside the functions that
# need them: they are slow to import and optional for the rest of the project.

HERE = Path(__file__).resolve().parents[2]  # project root
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"


def find_token() -> str | None:
    """HF token from $HF_TOKEN or <project root>/.hf_token, or None."""
    tok = os.environ.get("HF_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    tok_file = HERE / ".hf_token"
    if tok_file.exists():
        return tok_file.read_text().strip() or None
    return None


def read_token() -> str:
    tok = find_token()
    if not tok:
        sys.exit("ERROR: no HF token (set HF_TOKEN or create .hf_token)")
    return tok


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


TORCH_CUDA_HINT = ("an NVIDIA GPU is present but the installed PyTorch is CPU-only, so "
                   "speaker detection runs on the CPU. For the GPU install the CUDA build: "
                   "pip install --force-reinstall torch torchaudio "
                   "--index-url https://download.pytorch.org/whl/cu126")


def nvidia_gpu_present() -> bool:
    """True when an NVIDIA GPU with a working driver is visible (via CTranslate2)."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 - not installed / no driver
        return False


def diarize_device(requested: str = "auto") -> tuple[str, str | None]:
    """(device, hint) for pyannote: "cuda" when PyTorch can use a GPU, else "cpu".

    hint explains why the GPU is not used when there is one, else None.
    """
    import torch
    if requested == "cpu":
        return "cpu", None
    if torch.cuda.is_available():
        return "cuda", None
    if requested == "cuda":
        raise RuntimeError("--device cuda asked for, but PyTorch cannot use a GPU")
    return "cpu", TORCH_CUDA_HINT if nvidia_gpu_present() else None


def diarize_turns(audio: str | Path, token: str, num_speakers: int | None = None,
                  device: str = "auto",
                  on_device: Callable[[str, str | None], None] | None = None
                  ) -> list[tuple[float, float, str]]:
    """Who spoke when: [(start, end, "SPEAKER_00"), ...] for an audio file (wav is best).

    device: "auto" (GPU when PyTorch can use one, else CPU), "cuda" or "cpu". If the GPU
    run fails (e.g. out of memory) it is retried on the CPU.
    on_device(device, hint): called with the device used and, when a GPU is present but
    not usable, a hint how to fix that.
    Raises RuntimeError when pyannote isn't installed or the model can't be loaded.
    """
    try:
        import soundfile as sf
        import torch
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise RuntimeError("speaker detection needs pyannote.audio and soundfile: "
                           "pip install pyannote.audio soundfile") from exc

    dia = Pipeline.from_pretrained(DIARIZATION_MODEL, token=token)
    if dia is None:  # older pyannote returns None instead of raising
        raise RuntimeError(f"could not load {DIARIZATION_MODEL}; check the HF token and "
                           "accept the model's terms on huggingface.co")
    # Load audio in-memory via soundfile (avoids torchcodec / CUDA-lib issues).
    data, sr = sf.read(str(audio), dtype="float32", always_2d=True)  # (time, ch)
    waveform = torch.from_numpy(data.T.copy())  # -> (ch, time)
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}

    def run(dev: str):
        dia.to(torch.device(dev))
        return dia({"waveform": waveform, "sample_rate": sr}, **kwargs)

    dev, hint = diarize_device(device)
    if on_device:
        on_device(dev, hint)
    try:
        diarization = run(dev)
    except (RuntimeError, MemoryError) as exc:
        if dev != "cuda" or device == "cuda":
            raise
        torch.cuda.empty_cache()
        if on_device:
            on_device("cpu", f"GPU failed ({str(exc).splitlines()[0][:120]}); using the CPU")
        diarization = run("cpu")
    # pyannote v4 returns a DiarizeOutput; the Annotation is .speaker_diarization
    annotation = getattr(diarization, "speaker_diarization", diarization)
    return [(seg.start, seg.end, spk)
            for seg, _, spk in annotation.itertracks(yield_label=True)]


def main():
    from faster_whisper import WhisperModel

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
    try:
        turns = diarize_turns(in_path, token, args.num_speakers, on_device=lambda d, hint: print(
            f"      on {d}" + (f" - note: {hint}" if hint else "")))
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc}")
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

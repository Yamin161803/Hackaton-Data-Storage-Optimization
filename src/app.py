#!/usr/bin/env python3
"""One command from a raw video to meeting notes.

    python src/app.py talk.mp4            ->  data/meeting_notes/talk.md

Steps:
  1. video_splitter  - key frames + audio track
  2. transcribe      - speech to text with faster-whisper (GPU if present, else CPU)
     (--speakers)    - who is speaking, with pyannote (transcription/diarize.py)
  3. meeting_notes   - Markdown: summary (local model via Ollama) + each frame with its text

What is kept:
  data/meeting_notes/<video name>.md      the notes
  data/resources/<video name>/frame_*.jpg the frames, linked from the notes (relative
                                          paths, so keep the two folders together)
Audio and transcript are made in a temporary folder and deleted when the run ends.
Use --embed-images to put the frames inside the .md instead (one self-contained file).

Examples:
    python src/app.py talk.mp4
    python src/app.py talk.mp4 --whisper-model large-v3 --lang en
    python src/app.py talk.mp4 -o notes/talk.md --no-summary
    python src/app.py talk.mp4 --hint "Talk about GraalVM native image"
    python src/app.py meeting.mp4 --speakers --num-speakers 3
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

from notes.meeting_notes import build_notes
from notes.summarize import DEFAULT_MODEL, DEFAULT_URL, Summary, SummaryError, summarize
from video_splitter.splitter import CAMERA_THRESHOLD, DEFAULT_THRESHOLD, MODES, split_video

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
# notes: data/meeting_notes/<video name>.md, frames: data/resources/<video name>/
NOTES_DIR = DATA_DIR / "meeting_notes"
RESOURCES_DIR = DATA_DIR / "resources"


def transcribe_audio(audio: Path, model_name: str, device: str, lang: str | None,
                     batched: bool) -> Path:
    """Write <audio>.txt in the '[start -> end] text' format that meeting_notes reads."""
    from faster_whisper import WhisperModel  # slow import, only needed here

    from transcription.transcribe import HAVE_BATCHED, detect_device, transcribe_one

    def run(device: str, compute_type: str) -> Path:
        print(f"[2/3] transcribing with whisper '{model_name}' on {device} ({compute_type})...")
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        batched_model = None
        if batched and HAVE_BATCHED:
            from faster_whisper import BatchedInferencePipeline
            batched_model = BatchedInferencePipeline(model=model)
        return transcribe_one(model, batched_model, audio, lang, batched)

    requested = device
    device, compute_type = detect_device(device)
    try:
        return run(device, compute_type)
    except RuntimeError as exc:
        # A GPU is visible but its CUDA/cuDNN libraries are missing (e.g. cublas64_12.dll).
        if requested != "auto" or device != "cuda":
            raise
        print(f"      GPU failed ({exc}); falling back to CPU", file=sys.stderr)
        return run("cpu", "int8")


def add_speakers(audio: Path, transcript: Path, num_speakers: int | None = None) -> int:
    """Put a speaker label on every transcript line (as transcription.diarize writes them).

    Returns the number of speakers found. Raises RuntimeError if it can't be done.
    """
    from notes.meeting_notes import read_transcript
    from transcription.diarize import assign_speaker, diarize_turns, find_token

    token = find_token()
    if not token:
        raise RuntimeError("no Hugging Face token: set HF_TOKEN or create .hf_token "
                           "in the project folder")
    print("      finding who speaks when (pyannote)...")
    turns = diarize_turns(audio, token, num_speakers)
    lines = [f"[{s.start:7.2f} -> {s.end:7.2f}] {assign_speaker(s.start, s.end, turns)}: "
             f"{s.text}" for s in read_transcript(transcript)]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len({spk for *_, spk in turns})


def make_notes(video: Path, output: Path, *, threshold: float = DEFAULT_THRESHOLD,
               mode: str = "auto", camera_threshold: float = CAMERA_THRESHOLD,
               whisper_model: str = "base", device: str = "auto", lang: str | None = None,
               batched: bool = False, summary: bool = True, ollama_model: str = DEFAULT_MODEL,
               ollama_url: str = DEFAULT_URL, hint: str | None = None,
               resources_dir: Path | None = None, embed_images: bool = False,
               speakers: bool = False, num_speakers: int | None = None) -> Path:
    """Write the notes to `output` and return its path.

    The frames are moved to <resources_dir>/<video name>/ (default: data/resources) and
    linked from the notes, or, with embed_images, put inside the notes and not kept.
    speakers: label who says what (needs pyannote.audio and a Hugging Face token). If
    that fails the notes are still written, without speaker labels.
    """
    if not video.is_file():
        raise FileNotFoundError(video)

    with tempfile.TemporaryDirectory(prefix="video_notes_") as tmp:
        work = Path(tmp)

        print(f"[1/3] finding key frames and extracting audio from {video.name}...")
        split = split_video(video, work, "jpg", "wav", threshold=threshold, mode=mode,
                            camera_threshold=camera_threshold)
        print(f"      {split.mode} video ({split.motion:.0%} of frames moving, "
              f"threshold {split.threshold:g}): {split.frame_count} frames")

        labelled = False
        if split.audio_path:
            transcript = transcribe_audio(split.audio_path, whisper_model, device, lang,
                                          batched)
            if speakers:
                try:
                    found = add_speakers(split.audio_path, transcript, num_speakers)
                    labelled = True
                    print(f"      {found} speaker{'s' if found != 1 else ''}")
                except RuntimeError as exc:
                    # Don't throw away the transcription because diarization failed.
                    print(f"      warning: no speaker labels ({exc})", file=sys.stderr)
        else:
            print("[2/3] video has no audio track - notes will have frames only")
            (work / "audio.txt").write_text("", encoding="utf-8")

        def summarizer(text: str) -> Summary | None:
            if not summary or not text.strip():
                return None
            print(f"      summarizing with {ollama_model} via Ollama "
                  "(can take a few minutes)...")
            try:
                return summarize(text, model=ollama_model, url=ollama_url, hint=hint)
            except SummaryError as exc:
                # Don't throw away a long transcription because Ollama isn't running.
                print(f"      warning: no summary ({exc})", file=sys.stderr)
                return None

        frames_dir = None
        if not embed_images:
            frames_dir = save_frames(split.frames,
                                     Path(resources_dir or RESOURCES_DIR) / video.stem)
            print(f"      frames -> {frames_dir}")

        print("[3/3] writing notes...")
        return build_notes(work, output=output, title=video.stem.replace("_", " "),
                           summarizer=summarizer, embed_images=embed_images,
                           frames_dir=frames_dir, speakers=labelled)


def save_frames(frames, dest: Path) -> Path:
    """Move the frame images to `dest`, replacing frames from an earlier run."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("frame_*"):
        if old.is_file():
            old.unlink()
    for frame in frames:
        shutil.move(str(frame.path), dest / frame.path.name)
    return dest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Turn a raw video into Markdown notes (frames + transcript + summary). "
                    "Saves the notes and the frames; audio and transcript are not kept.")
    p.add_argument("video", type=Path, help="input video, e.g. talk.mp4")
    p.add_argument("-o", "--output", type=Path,
                   help="notes file to write (default: data/meeting_notes/<video name>.md)")
    p.add_argument("--resources", type=Path,
                   help="folder for the frames; they go in <resources>/<video name>/ "
                        "(default: data/resources)")
    p.add_argument("--embed-images", action="store_true",
                   help="put the frames inside the notes file instead of saving them")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"share of the picture (0-1) that must change for a new frame "
                        f"(default: {DEFAULT_THRESHOLD:g}); lower = more frames")
    p.add_argument("--mode", choices=MODES, default="auto",
                   help="auto (default) detects camera footage and uses --camera-threshold")
    p.add_argument("--camera-threshold", type=float, default=CAMERA_THRESHOLD,
                   help=f"threshold for camera footage (default: {CAMERA_THRESHOLD:g})")
    p.add_argument("--whisper-model", default="base",
                   help="tiny|base|small|medium|large-v3|distil-large-v3 (default: base)")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    p.add_argument("--lang", help="force language code (en, sv, ...). Default: auto-detect")
    p.add_argument("--batched", action="store_true", help="batched whisper (faster on GPU)")
    p.add_argument("--speakers", action="store_true",
                   help="label who is speaking (needs pyannote.audio + a Hugging Face token)")
    p.add_argument("--num-speakers", type=int,
                   help="exact number of speakers, if known (better speaker labels)")
    p.add_argument("--no-summary", action="store_true", help="skip the Ollama summary")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Ollama model for the summary (default: {DEFAULT_MODEL})")
    p.add_argument("--ollama-url", default=DEFAULT_URL, help=f"default: {DEFAULT_URL}")
    p.add_argument("--hint", help="context for the summary, e.g. correct spelling of names")
    args = p.parse_args(argv)

    output = args.output or NOTES_DIR / f"{args.video.stem}.md"
    t0 = time.time()
    try:
        md = make_notes(args.video, output, threshold=args.threshold, mode=args.mode,
                        camera_threshold=args.camera_threshold,
                        whisper_model=args.whisper_model, device=args.device, lang=args.lang,
                        batched=args.batched, summary=not args.no_summary,
                        ollama_model=args.model, ollama_url=args.ollama_url, hint=args.hint,
                        resources_dir=args.resources, embed_images=args.embed_images,
                        speakers=args.speakers or bool(args.num_speakers),
                        num_speakers=args.num_speakers)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"done in {time.time() - t0:.0f}s -> {md} ({md.stat().st_size / 1e3:,.0f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

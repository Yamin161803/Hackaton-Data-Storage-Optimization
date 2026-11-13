#!/usr/bin/env python3
"""Build a Markdown document from a split video folder: each saved frame followed by
the transcript spoken while that frame was on screen.

Input is the folder made by video_splitter + transcription.transcribe:
    <name>_split/
        frames/timestamps.csv     (from video_splitter)
        frames/frame_*.jpg
        audio.txt                 (from transcription.transcribe: "[  12.34 ->   18.90] text")

A transcript segment goes under the frame that was on screen for most of that segment.

Video identity: when the video's hash id is given (see videohash.py), it is written at the
top of the notes as front matter, so notes can be matched to their video:
    ---
    video: talk.mp4
    video_id: -4315225046207543519
    video_id_method: content
    ---

With --speakers the transcript has speaker labels, as written by transcription.diarize:
"[  12.34 ->   18.90] SPEAKER_00: text". The labels become "Speaker 1", "Speaker 2", ...
(numbered in the order they first speak) and the text is grouped by speaker.

At the top, a summary (overview + key points) is written by a local model via Ollama
(see notes/summarize.py). Use --no-summary to skip it.

Examples (run from the src folder):
    python -m notes.meeting_notes "../data/GraalVMs_Ahead-of-Time_Compilation_(Source)_split"
    python -m notes.meeting_notes ../data/talk_split -o talk.md --title "My talk"
    python -m notes.meeting_notes ../data/talk_split --hint "Talk about GraalVM native image"
"""
from __future__ import annotations

import argparse
import base64
import bisect
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote

from .summarize import DEFAULT_MODEL, DEFAULT_URL, Summary, SummaryError, summarize

_SEGMENT_RE = re.compile(r"^\[\s*([\d.]+)\s*->\s*([\d.]+)\]\s?(.*)$")
# pyannote labels: SPEAKER_00, SPEAKER_01, ... and UNKNOWN when nobody overlaps
_SPEAKER_RE = re.compile(r"^(SPEAKER_\d+|UNKNOWN):\s*(.*)$")


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None   # e.g. "Speaker 1", when the transcript has labels


@dataclass
class Section:
    seconds: float
    image: Path | None            # None for audio-only recordings
    segments: list[Segment] = field(default_factory=list)


def hms(seconds: float) -> str:
    """75.4 -> '00:01:15'."""
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def read_frames(timestamps_csv: Path, image_dir: Path | None = None) -> list[Section]:
    """image_dir: where the frame images are (default: next to timestamps.csv)."""
    with timestamps_csv.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"no frames listed in {timestamps_csv}")
    image_dir = image_dir or timestamps_csv.parent
    sections = [Section(float(r["seconds"]), image_dir / r["file"]) for r in rows]
    return sorted(sections, key=lambda s: s.seconds)


def read_transcript(path: Path, speakers: bool = False) -> list[Segment]:
    """speakers: lines carry a "SPEAKER_00: " label (from transcription.diarize)."""
    segments = []
    names: dict[str, str] = {}   # SPEAKER_03 -> "Speaker 1", in order of first speech
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        m = _SEGMENT_RE.match(line)
        if not m:
            raise ValueError(f"{path}:{n}: not a transcript line: {line!r}")
        text, speaker = m[3].strip(), None
        if speakers and (sm := _SPEAKER_RE.match(text)):
            label, text = sm[1], sm[2].strip()
            if label == "UNKNOWN":
                speaker = "Unknown"
            else:
                speaker = names.setdefault(label, f"Speaker {len(names) + 1}")
        if text:
            segments.append(Segment(float(m[1]), float(m[2]), text, speaker))
    return segments


def speaker_blocks(segments: list[Segment]) -> list[tuple[str | None, str]]:
    """Join consecutive segments of the same speaker: [(speaker, text), ...]."""
    blocks: list[tuple[str | None, str]] = []
    for seg in segments:
        if blocks and blocks[-1][0] == seg.speaker:
            blocks[-1] = (seg.speaker, blocks[-1][1] + " " + seg.text)
        else:
            blocks.append((seg.speaker, seg.text))
    return blocks


def assign(sections: list[Section], segments: list[Segment]) -> None:
    """Put each segment under the frame that was on screen for most of it.

    Whisper segments can be 10+ seconds long and cross a frame change, so the segment
    goes to the frame it overlaps the most (ties -> the earlier frame). Zero-length
    segments go to the frame showing when they start.
    """
    starts = [s.seconds for s in sections]
    for seg in segments:
        first = max(bisect.bisect_right(starts, seg.start + 1e-6) - 1, 0)
        best, best_overlap = first, -1.0
        for i in range(first, len(sections)):
            if i > first and starts[i] >= seg.end:
                break
            frame_start = starts[i] if i > first else min(starts[i], seg.start)
            frame_end = starts[i + 1] if i + 1 < len(sections) else float("inf")
            overlap = min(seg.end, frame_end) - max(seg.start, frame_start)
            if overlap > best_overlap:
                best, best_overlap = i, overlap
        sections[best].segments.append(seg)


def _link(target: Path, md_dir: Path) -> str:
    try:
        rel = Path(os.path.relpath(target.resolve(), md_dir.resolve())).as_posix()
    except ValueError:  # Windows: image and notes on different drives
        return target.resolve().as_uri()
    return quote(rel)  # escapes spaces and parentheses, which break Markdown links


_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def _data_uri(image: Path) -> str:
    """The image itself inside the link, so the Markdown file needs no image files."""
    mime = _MIME.get(image.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(image.read_bytes()).decode('ascii')}"


@dataclass
class VideoInfo:
    """Which video the notes come from (see videohash.py).

    size, quick_hash, full_hash: the file's fingerprint, written first in the notes so
    the video can be found from the notes and the notes from the video (lookup.py).
    id: 8-byte content hash from videohash.video_id (metadata ignored).
    """
    name: str
    id: int
    id_method: str = "content"   # "content" (metadata ignored) or "file" (whole file)
    size: int | None = None      # bytes
    quick_hash: str | None = None
    quick_hash_method: str | None = None
    full_hash: str | None = None
    full_hash_algorithm: str | None = None


def _front_matter(video: VideoInfo, speakers: int = 0) -> list[str]:
    lines = ["---"]
    # 1. fingerprint of the file: size, quick hash + method, full hash + algorithm
    if video.size is not None:
        lines.append(f"file_size: {video.size}")
    if video.quick_hash:
        lines += [f"quick_hash: {video.quick_hash}",
                  f"quick_hash_method: {video.quick_hash_method}"]
    if video.full_hash:
        lines += [f"full_hash: {video.full_hash}",
                  f"full_hash_algorithm: {video.full_hash_algorithm}"]
    # 2. name and content id
    lines += [f"video: {json.dumps(video.name, ensure_ascii=False)}",
              f"video_id: {video.id}", f"video_id_method: {video.id_method}"]
    if speakers:
        lines.append(f"speakers: {speakers}")   # notes have speaker labels
    return lines + ["---", ""]


_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def read_front_matter(md_path: str | Path) -> dict[str, str]:
    """The "key: value" lines at the top of a notes file ({} if there are none)."""
    try:
        with open(md_path, encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return {}
    m = _FRONT_MATTER_RE.match(head.replace("\r\n", "\n"))
    if not m:
        return {}
    return {k.strip(): v.strip() for k, sep, v in
            (line.partition(":") for line in m[1].splitlines()) if sep}


def read_video_id(md_path: str | Path) -> int | None:
    """The video_id written in a notes file's front matter, or None."""
    value = read_front_matter(md_path).get("video_id", "")
    return int(value) if re.fullmatch(r"-?\d+", value) else None


def read_speaker_count(md_path: str | Path) -> int:
    """How many speakers the notes are labelled with (0 = no speaker labels)."""
    value = read_front_matter(md_path).get("speakers", "")
    return int(value) if value.isdigit() else 0


def find_notes(notes_dir: str | Path, video_id: int) -> Path | None:
    """Notes in notes_dir that were made from the video with this id, if any."""
    for md in sorted(Path(notes_dir).glob("*.md")):
        if read_video_id(md) == video_id:
            return md
    return None


_IMAGE_LINK_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")


def embed_local_images(md_text: str, md_dir: str | Path) -> str:
    """Replace links to local image files with the images themselves (base64).

    Turns notes that link to data/resources into one self-contained file, e.g. for a
    download. Links that are already data: URIs, web links or missing files stay as is.
    """
    def repl(m: re.Match) -> str:
        link = m[2]
        if re.match(r"^[a-z][a-z0-9+.-]*:", link, re.I) and not link.startswith("file:"):
            return m[0]                       # data:, http:, https: ...
        path = (Path(unquote(link.removeprefix("file:///"))) if link.startswith("file:")
                else Path(md_dir) / unquote(link))
        if not path.is_file():
            return m[0]
        return m[1] + _data_uri(path) + m[3]
    return _IMAGE_LINK_RE.sub(repl, md_text)


def render(sections: list[Section], md_path: Path, title: str,
           video_end: float | None, summary: Summary | None = None,
           embed_images: bool = False, video: VideoInfo | None = None) -> str:
    md_dir = md_path.parent
    total = video_end if video_end is not None else sections[-1].seconds
    speakers = sorted({seg.speaker for sec in sections for seg in sec.segments
                       if seg.speaker and seg.speaker != "Unknown"})
    if any(sec.image for sec in sections):
        info = f"{len(sections)} frames · {hms(total)} long"
    else:
        info = f"audio only · {len(sections)} parts · {hms(total)} long"
    if speakers:
        info += f" · {len(speakers)} speaker{'s' if len(speakers) != 1 else ''}"
    lines = _front_matter(video, len(speakers)) if video else []
    lines += [f"# {title}", "", info, ""]
    if video:
        lines += [f"Video: `{video.name}` · ID `{video.id}`", ""]

    if summary:
        lines += ["## Summary", "", summary.overview, "", "### Key points", ""]
        lines += [f"- {p}" for p in summary.key_points]
        lines += ["", f"_Summary generated by {summary.model} (local, via Ollama) from the "
                      "automatic transcript. Check details against the transcript below._", ""]

    for i, sec in enumerate(sections, start=1):
        end = sections[i].seconds if i < len(sections) else total
        lines += ["---", "", f"## {i}. {hms(sec.seconds)} – {hms(end)}", ""]
        if sec.image is not None:
            lines += [f"![Frame at {hms(sec.seconds)}]"
                      f"({_data_uri(sec.image) if embed_images else _link(sec.image, md_dir)})",
                      ""]
        if not sec.segments:
            lines += ["_(no speech)_", ""]
        for speaker, text in speaker_blocks(sec.segments):
            lines += [f"**{speaker}:** {text}" if speaker else text, ""]
    return "\n".join(lines)


def build_notes(split_dir: str | Path, output: str | Path | None = None,
                transcript: str | Path | None = None, title: str | None = None,
                summarizer: Callable[[str], Summary | None] | None = None,
                embed_images: bool = False, frames_dir: str | Path | None = None,
                speakers: bool = False, video: VideoInfo | None = None,
                part_seconds: float | None = None) -> Path:
    """Write the Markdown file and return its path (default: <split_dir>/notes.md).

    summarizer: called with the full transcript text; its Summary goes at the top.
    None = no summary.
    embed_images: put the frames inside the file (base64) instead of linking to them.
    frames_dir: folder with the frame images, if they were moved away from
    <split_dir>/frames (timestamps.csv is still read from there).
    speakers: the transcript has speaker labels (see read_transcript).
    video: which video the notes come from; its hash id goes at the top of the notes.
    part_seconds: audio-only recordings (no frames): split the text into parts of this
    many seconds instead of one part per frame; frames/timestamps.csv is not needed.
    """
    split_dir = Path(split_dir)
    timestamps_csv = split_dir / "frames" / "timestamps.csv"
    transcript = Path(transcript) if transcript else split_dir / "audio.txt"
    needed = [transcript] if part_seconds else [timestamps_csv, transcript]
    for p in needed:
        if not p.is_file():
            raise FileNotFoundError(p)
    md_path = Path(output) if output else split_dir / "notes.md"
    if title is None:
        name = split_dir.resolve().name
        title = name.removesuffix("_split").replace("_", " ")

    segments = read_transcript(transcript, speakers)
    if part_seconds:
        end = max([s.end for s in segments], default=0.0)
        count = max(1, int(-(-end // part_seconds)))      # ceil, at least one part
        sections = [Section(i * part_seconds, None) for i in range(count)]
    else:
        sections = read_frames(timestamps_csv, Path(frames_dir) if frames_dir else None)
    assign(sections, segments)
    video_end = max([sections[-1].seconds] + [s.end for s in segments])
    summary = summarizer(transcript_text(segments)) if summarizer else None

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render(sections, md_path, title, video_end, summary, embed_images,
                              video), encoding="utf-8")
    return md_path


def transcript_text(segments: list[Segment]) -> str:
    """Plain text for the summary; one "Speaker 1: ..." line per turn when labelled."""
    if not any(seg.speaker for seg in segments):
        return " ".join(seg.text for seg in segments)
    return "\n".join(f"{spk or 'Unknown'}: {text}" for spk, text in speaker_blocks(segments))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Make a Markdown document of frames + transcript from a split video folder.")
    p.add_argument("split_dir", help="folder with frames/timestamps.csv and audio.txt")
    p.add_argument("-o", "--output", help="Markdown file to write (default: <split_dir>/notes.md)")
    p.add_argument("--transcript", help="transcript file (default: <split_dir>/audio.txt)")
    p.add_argument("--title", help="document title (default: from the folder name)")
    p.add_argument("--speakers", action="store_true",
                   help="transcript has speaker labels (from transcription.diarize)")
    p.add_argument("--no-summary", action="store_true", help="don't write a summary at the top")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Ollama model for the summary (default: {DEFAULT_MODEL})")
    p.add_argument("--ollama-url", default=DEFAULT_URL, help=f"default: {DEFAULT_URL}")
    p.add_argument("--hint", help="extra context for the summary, e.g. correct spelling of "
                                  "names and terms the transcript gets wrong")
    args = p.parse_args(argv)

    summarizer = None
    if not args.no_summary:
        print(f"summarizing with {args.model} via Ollama (can take a few minutes)...")
        def summarizer(text):  # noqa: E306
            return summarize(text, model=args.model, url=args.ollama_url, hint=args.hint)
    try:
        md = build_notes(args.split_dir, args.output, args.transcript, args.title, summarizer,
                         speakers=args.speakers)
    except (FileNotFoundError, ValueError, SummaryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, SummaryError):
            print("(use --no-summary to create the notes without a summary)", file=sys.stderr)
        return 1
    print(f"notes -> {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

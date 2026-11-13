"""Split an MP4 (or any ffmpeg-readable video) into key frames and an audio track.

Frame selection: the first frame is always saved. After that, a frame is saved only
when it differs enough from the last saved frame (a "big change", e.g. a new slide).

This runs in two passes over the video:
  1. ffmpeg decodes every frame to a tiny grayscale image; Python compares each one
     with the last saved frame and picks which frames to keep.
  2. ffmpeg writes only the picked frames, at full resolution.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

IMAGE_FORMATS = ("jpg", "png")
AUDIO_FORMATS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "flac": ["-c:a", "flac"],
}

# fraction of the picture that must change (0.05 = 5 %)
DEFAULT_THRESHOLD = 0.05

# Frames are compared at this size, which ignores compression noise and tiny details.
_CMP_W, _CMP_H = 160, 90
# A pixel counts as "changed" when its gray level moves by more than this (0-255).
_PIXEL_DIFF = 25


@dataclass
class Frame:
    index: int        # 1-based, in output order
    seconds: float    # presentation time in the video
    path: Path
    reason: str       # "first" or "change"
    change: float     # fraction of the picture that differs from the previous saved frame

    @property
    def timestamp(self) -> str:
        return format_timestamp(self.seconds)


@dataclass
class SplitResult:
    frames_dir: Path
    audio_path: Path | None  # None when the video has no audio stream
    timestamps_csv: Path
    frames: list[Frame] = field(default_factory=list)

    @property
    def frame_count(self) -> int:
        return len(self.frames)


def format_timestamp(seconds: float) -> str:
    """12.5 -> '00:00:12.500' (HH:MM:SS.mmm)."""
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _filename_timestamp(seconds: float) -> str:
    """Windows-safe version for file names: '00h00m12.500s'."""
    h, m, rest = format_timestamp(seconds).split(":")
    return f"{h}h{m}m{rest}s"


# showinfo logs one line per frame, e.g. "... n:  41 pts: 41 pts_time:41  duration:..."
_SHOWINFO_RE = re.compile(r"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:(-?[\d.]+)")


def find_ffmpeg() -> str:
    """Use a system ffmpeg if present, otherwise the one bundled with imageio-ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "ffmpeg not found. Install it, or run: pip install imageio-ffmpeg"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True,
                          text=True, errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr.strip()[-2000:]}")
    return proc


def has_audio(video: Path, ffmpeg: str) -> bool:
    # `ffmpeg -i` with no output exits non-zero but prints stream info to stderr.
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video)],
        capture_output=True, text=True, errors="replace",
    )
    return re.search(r"Stream #\S+.*: Audio:", proc.stderr) is not None


def change_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of pixels whose gray level differs by more than _PIXEL_DIFF."""
    return float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > _PIXEL_DIFF).mean())


class _TimestampReader(threading.Thread):
    """Reads ffmpeg's stderr in the background and collects showinfo timestamps.

    showinfo sits before the scaler, so frame n's timestamp is logged before frame n's
    pixels reach stdout; get(n) waits for it.
    """

    def __init__(self, stream):
        super().__init__(daemon=True)
        self._stream = stream
        self._times: dict[int, float] = {}
        self._done = False
        self._cond = threading.Condition()
        self.tail: list[str] = []  # last log lines, for error messages

    def run(self):
        for raw in self._stream:
            line = raw.decode(errors="replace")
            self.tail = (self.tail + [line])[-30:]
            if m := _SHOWINFO_RE.search(line):
                with self._cond:
                    self._times[int(m[1])] = float(m[2])
                    self._cond.notify_all()
        with self._cond:
            self._done = True
            self._cond.notify_all()

    def get(self, n: int) -> float:
        with self._cond:
            self._cond.wait_for(lambda: n in self._times or self._done)
            if n not in self._times:
                raise RuntimeError(
                    f"ffmpeg reported no timestamp for frame {n + 1}")
            return self._times.pop(n)


def _pick_frames(video: Path, ffmpeg: str, threshold: float):
    """Pass 1. Returns [(frame_number, seconds, reason, change)], frame_number 0-based."""
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(video), "-an",
           "-vf", f"showinfo,scale={_CMP_W}:{_CMP_H}:flags=area,format=gray",
           "-fps_mode", "passthrough", "-f", "rawvideo", "-"]
    size = _CMP_W * _CMP_H
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    reader = _TimestampReader(proc.stderr)
    reader.start()

    picked = []
    last_img = None
    n = 0
    try:
        while len(buf := proc.stdout.read(size)) == size:
            img = np.frombuffer(buf, dtype=np.uint8)
            t = reader.get(n)
            if last_img is None:
                picked.append((n, t, "first", 0.0))
                last_img = img
            elif (change := change_ratio(img, last_img)) >= threshold:
                picked.append((n, t, "change", change))
                last_img = img
            n += 1
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        reader.join()
    if returncode != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(reader.tail).strip())
    if not picked:
        raise RuntimeError("no video frames found")
    return picked


def split_video(
    video: str | Path,
    output_dir: str | Path,
    image_format: str = "jpg",
    audio_format: str = "wav",
    threshold: float = DEFAULT_THRESHOLD,
) -> SplitResult:
    """Save key frames to <output_dir>/frames/ and audio to <output_dir>/audio.<ext>.

    The first frame is always saved; after that a frame is saved only when at least
    `threshold` (0-1) of the picture differs from the last saved frame.

    Frame files are named by timestamp, e.g. frame_00h00m41.000s.jpg, and
    <output_dir>/frames/timestamps.csv lists them with the reason each was saved.
    """
    video = Path(video)
    output_dir = Path(output_dir)
    if not video.is_file():
        raise FileNotFoundError(video)
    if image_format not in IMAGE_FORMATS:
        raise ValueError(f"image_format must be one of {IMAGE_FORMATS}")
    if audio_format not in AUDIO_FORMATS:
        raise ValueError(f"audio_format must be one of {tuple(AUDIO_FORMATS)}")
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be between 0 and 1")

    ffmpeg = find_ffmpeg()
    picked = _pick_frames(video, ffmpeg, threshold)

    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # Clear frames from an earlier run so a re-run with other settings doesn't mix files.
    for ext in IMAGE_FORMATS:
        for old in frames_dir.glob(f"frame_*.{ext}"):
            old.unlink()

    # Pass 2: write only the picked frames. The select expression can get long, so it is
    # read from a file ("-/vf <file>") instead of the command line.
    select = "select=" + "+".join(f"eq(n\\,{n})" for n, *_ in picked)
    with tempfile.TemporaryDirectory() as tmp:
        filter_file = Path(tmp) / "select.txt"
        filter_file.write_text(select, encoding="utf-8")
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-an",
               "-/vf", str(filter_file), "-fps_mode", "passthrough"]
        if image_format == "jpg":
            cmd += ["-q:v", "2"]
        cmd.append(str(frames_dir / f"frame_%06d.{image_format}"))
        _run(cmd)

    frames: list[Frame] = []
    for index, (_, seconds, reason, change) in enumerate(picked, start=1):
        raw = frames_dir / f"frame_{index:06d}.{image_format}"
        if not raw.is_file():
            raise RuntimeError(
                f"ffmpeg did not write frame {index} ({format_timestamp(seconds)})")
        named = raw.with_name(
            f"frame_{_filename_timestamp(seconds)}.{image_format}")
        if frames and named == frames[-1].path:
            raise RuntimeError(
                f"two frames share timestamp {format_timestamp(seconds)}")
        os.replace(raw, named)
        frames.append(Frame(index, seconds, named, reason, change))

    timestamps_csv = frames_dir / "timestamps.csv"
    with timestamps_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "seconds", "timestamp",
                        "file", "reason", "change"])
        for fr in frames:
            writer.writerow([fr.index, f"{fr.seconds:.3f}", fr.timestamp, fr.path.name,
                             fr.reason, f"{fr.change:.3f}"])

    audio_path = None
    if has_audio(video, ffmpeg):
        audio_path = output_dir / f"audio.{audio_format}"
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(
                video),
            "-vn", *AUDIO_FORMATS[audio_format], str(audio_path),
        ])

    return SplitResult(frames_dir=frames_dir, audio_path=audio_path,
                       timestamps_csv=timestamps_csv, frames=frames)

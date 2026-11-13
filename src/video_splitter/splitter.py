"""Split an MP4 (or any ffmpeg-readable video) into image frames and an audio track."""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_FORMATS = ("jpg", "png")
DEFAULT_EVERY = 30  # keep 1 frame out of every 30 unless told otherwise
AUDIO_FORMATS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "flac": ["-c:a", "flac"],
}


@dataclass
class Frame:
    index: int        # 1-based, in output order
    seconds: float    # presentation time in the video
    path: Path

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
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
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


def split_video(
    video: str | Path,
    output_dir: str | Path,
    fps: float | None = None,
    image_format: str = "jpg",
    audio_format: str = "wav",
    every: int | None = None,
) -> SplitResult:
    """Write frames to <output_dir>/frames/ and audio to <output_dir>/audio.<ext>.

    Each frame file is named with its timestamp, e.g. frame_00h00m41.000s.jpg,
    and <output_dir>/frames/timestamps.csv lists them all.

    every: keep 1 frame out of every N video frames (frames 1, N+1, 2N+1, ...).
           Defaults to 30; use 1 for all frames.
    fps:   alternative to `every`: sample this many frames per second of video.
           Can't be combined with `every`.
    """
    video = Path(video)
    output_dir = Path(output_dir)
    if not video.is_file():
        raise FileNotFoundError(video)
    if image_format not in IMAGE_FORMATS:
        raise ValueError(f"image_format must be one of {IMAGE_FORMATS}")
    if audio_format not in AUDIO_FORMATS:
        raise ValueError(f"audio_format must be one of {tuple(AUDIO_FORMATS)}")
    if fps is not None and fps <= 0:
        raise ValueError("fps must be positive")
    if fps is not None and every is not None:
        raise ValueError("use either fps or every, not both")
    if every is None and fps is None:
        every = DEFAULT_EVERY
    if every is not None and every < 1:
        raise ValueError("every must be 1 or more")

    ffmpeg = find_ffmpeg()
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    # Clear frames from an earlier run so a re-run with another fps doesn't mix files.
    for ext in IMAGE_FORMATS:
        for old in frames_dir.glob(f"frame_*.{ext}"):
            old.unlink()

    # showinfo (last in the filter chain) logs the pts_time of every frame that gets written.
    if fps is not None:
        vf = f"fps={fps},showinfo"
    elif every > 1:
        vf = f"select=not(mod(n\\,{every})),showinfo"  # n is the 0-based input frame number
    else:
        vf = "showinfo"
    frame_cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "info", "-y",
                 "-i", str(video), "-an", "-vf", vf]
    if fps is None:
        frame_cmd += ["-fps_mode", "passthrough"]
    if image_format == "jpg":
        frame_cmd += ["-q:v", "2"]
    frame_cmd.append(str(frames_dir / f"frame_%06d.{image_format}"))
    proc = _run(frame_cmd)
    times = {int(n): float(t) for n, t in _SHOWINFO_RE.findall(proc.stderr)}

    frames: list[Frame] = []
    index = 1
    while (raw := frames_dir / f"frame_{index:06d}.{image_format}").is_file():
        seconds = times.get(index - 1)  # showinfo counts from 0
        if seconds is None:
            raise RuntimeError(f"ffmpeg reported no timestamp for frame {index}")
        named = raw.with_name(f"frame_{_filename_timestamp(seconds)}.{image_format}")
        if frames and named == frames[-1].path:
            raise RuntimeError(f"two frames share timestamp {format_timestamp(seconds)}")
        os.replace(raw, named)
        frames.append(Frame(index=index, seconds=seconds, path=named))
        index += 1

    timestamps_csv = frames_dir / "timestamps.csv"
    with timestamps_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["frame", "seconds", "timestamp", "file"])
        for fr in frames:
            writer.writerow([fr.index, f"{fr.seconds:.3f}", fr.timestamp, fr.path.name])

    audio_path = None
    if has_audio(video, ffmpeg):
        audio_path = output_dir / f"audio.{audio_format}"
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
            "-vn", *AUDIO_FORMATS[audio_format], str(audio_path),
        ])

    return SplitResult(frames_dir=frames_dir, audio_path=audio_path,
                       timestamps_csv=timestamps_csv, frames=frames)

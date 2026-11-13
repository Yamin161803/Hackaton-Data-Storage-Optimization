"""Split an MP4 (or any ffmpeg-readable video) into image frames and an audio track."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

IMAGE_FORMATS = ("jpg", "png")
AUDIO_FORMATS = {
    "wav": ["-c:a", "pcm_s16le"],
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "flac": ["-c:a", "flac"],
}


@dataclass
class SplitResult:
    frames_dir: Path
    frame_count: int
    audio_path: Path | None  # None when the video has no audio stream


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
) -> SplitResult:
    """Write frames to <output_dir>/frames/ and audio to <output_dir>/audio.<ext>.

    fps: frames per second to extract; None keeps every frame of the video.
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

    ffmpeg = find_ffmpeg()
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    frame_cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-an"]
    if fps is not None:
        frame_cmd += ["-vf", f"fps={fps}"]
    else:
        frame_cmd += ["-fps_mode", "passthrough"]
    if image_format == "jpg":
        frame_cmd += ["-q:v", "2"]
    frame_cmd.append(str(frames_dir / f"frame_%06d.{image_format}"))
    _run(frame_cmd)
    frame_count = len(list(frames_dir.glob(f"frame_*.{image_format}")))

    audio_path = None
    if has_audio(video, ffmpeg):
        audio_path = output_dir / f"audio.{audio_format}"
        _run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
            "-vn", *AUDIO_FORMATS[audio_format], str(audio_path),
        ])

    return SplitResult(frames_dir=frames_dir, frame_count=frame_count, audio_path=audio_path)

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_splitter import split_video  # noqa: E402
from video_splitter.splitter import find_ffmpeg  # noqa: E402


def make_video(path: Path, with_audio: bool = True) -> Path:
    """Create a 2-second, 10 fps test video (20 frames)."""
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=2:size=64x48:rate=10"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True)
    return path


def test_splits_all_frames_and_audio(tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    result = split_video(video, tmp_path / "out")
    assert result.frame_count == 20
    assert (result.frames_dir / "frame_000001.jpg").is_file()
    assert result.audio_path == tmp_path / "out" / "audio.wav"
    assert result.audio_path.stat().st_size > 0


def test_fps_and_formats(tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    result = split_video(video, tmp_path / "out", fps=2, image_format="png", audio_format="mp3")
    assert result.frame_count == 4
    assert (result.frames_dir / "frame_000001.png").is_file()
    assert result.audio_path.suffix == ".mp3"


def test_video_without_audio(tmp_path):
    video = make_video(tmp_path / "silent.mp4", with_audio=False)
    result = split_video(video, tmp_path / "out")
    assert result.frame_count == 20
    assert result.audio_path is None


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        split_video(tmp_path / "nope.mp4", tmp_path / "out")

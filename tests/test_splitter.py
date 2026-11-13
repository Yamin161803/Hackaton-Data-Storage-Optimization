import csv
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_splitter import format_timestamp, split_video  # noqa: E402
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
    result = split_video(video, tmp_path / "out", every=1)
    assert result.frame_count == 20
    assert (result.frames_dir / "frame_000001_00h00m00.000s.jpg").is_file()
    assert result.audio_path == tmp_path / "out" / "audio.wav"
    assert result.audio_path.stat().st_size > 0


def test_frames_have_timestamps(tmp_path):
    video = make_video(tmp_path / "clip.mp4")  # 10 fps -> one frame every 0.1 s
    result = split_video(video, tmp_path / "out", every=1)
    assert [f.seconds for f in result.frames] == pytest.approx([i / 10 for i in range(20)])
    assert result.frames[15].timestamp == "00:00:01.500"
    assert result.frames[15].path.name == "frame_000016_00h00m01.500s.jpg"
    assert all(f.path.is_file() for f in result.frames)

    with result.timestamps_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 20
    assert rows[15] == {"frame": "16", "seconds": "1.500", "timestamp": "00:00:01.500",
                        "file": "frame_000016_00h00m01.500s.jpg"}


def test_fps_and_formats(tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    result = split_video(video, tmp_path / "out", fps=2, image_format="png", audio_format="mp3")
    assert result.frame_count == 4
    assert [f.seconds for f in result.frames] == pytest.approx([0, 0.5, 1.0, 1.5])
    assert (result.frames_dir / "frame_000002_00h00m00.500s.png").is_file()
    assert result.audio_path.suffix == ".mp3"


def test_rerun_replaces_old_frames(tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    split_video(video, tmp_path / "out", every=1)
    result = split_video(video, tmp_path / "out", fps=1)
    assert len(list(result.frames_dir.glob("frame_*"))) == result.frame_count == 2


def test_default_keeps_one_frame_per_30(tmp_path):
    # 4 s at 30 fps = 120 frames -> frames 0, 30, 60, 90 -> one per second
    video = tmp_path / "clip30.mp4"
    subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=4:size=64x48:rate=30",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    result = split_video(video, tmp_path / "out")
    assert [f.seconds for f in result.frames] == pytest.approx([0, 1, 2, 3])


def test_every_n_frames(tmp_path):
    video = make_video(tmp_path / "clip.mp4")  # 20 frames at 10 fps
    result = split_video(video, tmp_path / "out", every=5)
    assert [f.seconds for f in result.frames] == pytest.approx([0, 0.5, 1.0, 1.5])
    assert result.frames[1].path.name == "frame_000002_00h00m00.500s.jpg"


def test_every_and_fps_conflict(tmp_path):
    video = make_video(tmp_path / "clip.mp4")
    with pytest.raises(ValueError):
        split_video(video, tmp_path / "out", fps=1, every=5)
    with pytest.raises(ValueError):
        split_video(video, tmp_path / "out", every=0)


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00.000"
    assert format_timestamp(3723.4567) == "01:02:03.457"


def test_video_without_audio(tmp_path):
    video = make_video(tmp_path / "silent.mp4", with_audio=False)
    result = split_video(video, tmp_path / "out", every=1)
    assert result.frame_count == 20
    assert result.audio_path is None


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        split_video(tmp_path / "nope.mp4", tmp_path / "out")

import csv
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_splitter import change_ratio, format_timestamp, split_video  # noqa: E402
from video_splitter.splitter import find_ffmpeg  # noqa: E402

FPS = 10
BOX = "drawbox=x=0:y=0:w=16:h=12:color=white:t=fill"  # covers 1/16 = 6.25 % of a 64x48 frame


def make_video(path: Path, scenes, with_audio: bool = True) -> Path:
    """Build a 64x48, 10 fps video from scenes: [(lavfi source, seconds), ...].

    A source like "black" becomes color=c=black; anything with "=" or "," is used as is
    (e.g. "black,drawbox=...").
    """
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    for src, seconds in scenes:
        color, _, extra = src.partition(",")
        chain = f"color=c={color}:s=64x48:r={FPS}:d={seconds}" + (f",{extra}" if extra else "")
        cmd += ["-f", "lavfi", "-i", chain]
    total = sum(s for _, s in scenes)
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={total}"]
    inputs = "".join(f"[{i}:v]" for i in range(len(scenes)))
    cmd += ["-filter_complex", f"{inputs}concat=n={len(scenes)}:v=1:a=0[v]", "-map", "[v]"]
    if with_audio:
        cmd += ["-map", f"{len(scenes)}:a", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def times(result):
    return [f.seconds for f in result.frames]


def reasons(result):
    return [f.reason for f in result.frames]


def test_first_frame_and_big_changes(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 3), ("white", 3), ("black", 3)])
    result = split_video(video, tmp_path / "out")
    assert times(result) == pytest.approx([0, 3, 6])
    assert reasons(result) == ["first", "change", "change"]
    assert [f.path.name for f in result.frames] == [
        "frame_00h00m00.000s.jpg", "frame_00h00m03.000s.jpg", "frame_00h00m06.000s.jpg"]
    assert all(f.path.is_file() for f in result.frames)
    assert result.frames[1].change > 0.9


def test_no_change_saves_only_first_frame(tmp_path):
    # no timer: a long video that never changes gives just one frame
    video = make_video(tmp_path / "v.mp4", [("black", 40)])
    result = split_video(video, tmp_path / "out")
    assert times(result) == pytest.approx([0])
    assert reasons(result) == ["first"]


def test_threshold_decides_what_a_big_change_is(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 2), (f"black,{BOX}", 2)])
    default = split_video(video, tmp_path / "a")  # 6.25 % change >= 5 %
    assert times(default) == pytest.approx([0, 2])
    assert default.frames[1].change == pytest.approx(0.0625, abs=0.02)

    strict = split_video(video, tmp_path / "b", threshold=0.1)  # 6.25 % < 10 %
    assert times(strict) == pytest.approx([0])


def test_compares_with_last_saved_frame_not_previous_frame(tmp_path):
    # A slow fade: each frame is only a little brighter than the one before (never a big
    # change frame-to-frame), but it keeps drifting away from the last saved frame.
    video = make_video(tmp_path / "v.mp4", [("white,fade=in:st=0:d=4", 4)])
    result = split_video(video, tmp_path / "out")
    assert len(result.frames) >= 4
    assert reasons(result)[1:] == ["change"] * (len(result.frames) - 1)


def test_timestamps_csv(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 2), ("white", 2)])
    result = split_video(video, tmp_path / "out")
    with result.timestamps_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["timestamp"] for r in rows] == ["00:00:00.000", "00:00:02.000"]
    assert rows[1]["file"] == "frame_00h00m02.000s.jpg"
    assert rows[1]["reason"] == "change"
    assert float(rows[1]["change"]) > 0.9


def test_audio_and_formats(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 2)])
    wav = split_video(video, tmp_path / "a")
    assert wav.audio_path == tmp_path / "a" / "audio.wav"
    assert wav.audio_path.stat().st_size > 0

    other = split_video(video, tmp_path / "b", image_format="png", audio_format="mp3")
    assert other.frames[0].path.name == "frame_00h00m00.000s.png"
    assert other.audio_path.suffix == ".mp3"


def test_video_without_audio(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 2)], with_audio=False)
    result = split_video(video, tmp_path / "out")
    assert result.frame_count == 1
    assert result.audio_path is None


def test_rerun_replaces_old_frames(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 2), (f"black,{BOX}", 2)])
    assert split_video(video, tmp_path / "out").frame_count == 2
    result = split_video(video, tmp_path / "out", threshold=0.1)
    assert len(list(result.frames_dir.glob("frame_*"))) == result.frame_count == 1


def test_invalid_settings(tmp_path):
    video = make_video(tmp_path / "v.mp4", [("black", 1)])
    for kwargs in ({"threshold": 0}, {"threshold": 1.5}):
        with pytest.raises(ValueError):
            split_video(video, tmp_path / "out", **kwargs)


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        split_video(tmp_path / "nope.mp4", tmp_path / "out")


def test_change_ratio():
    a = np.zeros(100, dtype=np.uint8)
    b = a.copy()
    b[:10] = 200          # big change on 10 % of pixels
    b[10:20] = 20         # small change (below the pixel threshold) is ignored
    assert change_ratio(a, a) == 0
    assert change_ratio(a, b) == pytest.approx(0.1)


def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00.000"
    assert format_timestamp(3723.4567) == "01:02:03.457"

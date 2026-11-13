"""Tests for the one-command app (src/app.py) and the command-line entry points.

Speech recognition and Ollama are replaced by fakes, so these run fast and offline.
"""
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app  # noqa: E402
import faster_whisper  # noqa: E402
import video_splitter.__main__ as splitter_cli  # noqa: E402
from notes import meeting_notes  # noqa: E402
from notes.summarize import Summary, SummaryError, _chunks  # noqa: E402
from test_splitter import BOX, make_video  # noqa: E402
from transcription import transcribe  # noqa: E402


@pytest.fixture
def fake_transcribe(monkeypatch):
    """Replace whisper: writes a transcript next to the audio and remembers the call."""
    calls = []

    def fake(audio, model_name, device, lang, batched):
        calls.append(audio)
        out = audio.with_suffix(".txt")
        out.write_text("[   0.00 ->    2.00] First slide talk.\n"
                       "[   3.00 ->    5.00] Second slide talk.\n", encoding="utf-8")
        return out
    monkeypatch.setattr(app, "transcribe_audio", fake)
    return calls


@pytest.fixture(autouse=True)
def resources_in_tmp(tmp_path, monkeypatch):
    """Never write frames into the real data/resources during tests."""
    resources = tmp_path / "resources"
    monkeypatch.setattr(app, "RESOURCES_DIR", resources)
    return resources


# ---------- make_notes ----------

def test_make_notes_saves_notes_and_frames(tmp_path, fake_transcribe, resources_in_tmp):
    video = make_video(tmp_path / "My_talk.mp4", [("black", 2), ("white", 4)])
    out = tmp_path / "meeting_notes" / "My_talk.md"

    md = app.make_notes(video, out, summary=False)

    assert md == out
    frames = sorted(p.name for p in (resources_in_tmp / "My_talk").iterdir())
    assert frames == ["frame_00h00m00.000s.jpg", "frame_00h00m02.000s.jpg"]
    # nothing else is kept: no audio, transcript or timestamps.csv
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "My_talk.mp4", "meeting_notes", "resources"]
    text = md.read_text(encoding="utf-8")
    # notes start with the video's fingerprint: size, quick hash, full hash
    import hashlib
    full = hashlib.sha256(video.read_bytes()).hexdigest()
    head = text.split("\n---\n", 1)[0].splitlines()
    assert head[0] == "---"
    assert head[1] == f"file_size: {video.stat().st_size}"
    assert head[2].startswith("quick_hash: ") and len(head[2]) == len("quick_hash: ") + 64
    assert head[3] == "quick_hash_method: sha256-size-first-last-1MiB"
    assert head[4] == f"full_hash: {full}"
    assert head[5] == "full_hash_algorithm: sha256"
    assert head[6] == 'video: "My_talk.mp4"' and head[7].startswith("video_id: ")
    assert "\n---\n\n# My talk\n" in text
    assert "base64" not in text
    one, two = text.split("## 1.")[1].split("## 2.")
    assert "First slide talk." in one and "Second slide talk." in two
    # the temporary work folder is gone
    assert len(fake_transcribe) == 1 and not fake_transcribe[0].parent.exists()


def test_notes_link_to_frames_with_relative_paths(tmp_path, fake_transcribe):
    video = make_video(tmp_path / "My talk (v2).mp4", [("black", 2), ("white", 2)])
    md = app.make_notes(video, tmp_path / "meeting_notes" / "n.md", summary=False)
    text = md.read_text(encoding="utf-8")
    links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    assert links == [
        "../resources/My%20talk%20%28v2%29/frame_00h00m00.000s.jpg",
        "../resources/My%20talk%20%28v2%29/frame_00h00m02.000s.jpg"]
    for link in links:   # every link points at a real file
        assert (md.parent / unquote(link)).is_file()


def test_rerun_replaces_old_frames(tmp_path, fake_transcribe, resources_in_tmp):
    video = make_video(tmp_path / "v.mp4", [("black", 2), ("white", 2)])
    app.make_notes(video, tmp_path / "v.md", summary=False)
    assert len(list((resources_in_tmp / "v").iterdir())) == 2
    video = make_video(tmp_path / "v.mp4", [("black", 4)])
    app.make_notes(video, tmp_path / "v.md", summary=False)
    assert [p.name for p in (resources_in_tmp / "v").iterdir()] == ["frame_00h00m00.000s.jpg"]


def test_embed_images_keeps_no_frame_files(tmp_path, fake_transcribe, resources_in_tmp):
    video = make_video(tmp_path / "v.mp4", [("black", 2), ("white", 2)])
    text = app.make_notes(video, tmp_path / "v.md", summary=False, embed_images=True) \
        .read_text(encoding="utf-8")
    assert text.count("](data:image/jpeg;base64,") == 2
    assert not resources_in_tmp.exists()


def test_custom_resources_folder(tmp_path, fake_transcribe):
    video = make_video(tmp_path / "v.mp4", [("black", 2)])
    app.make_notes(video, tmp_path / "v.md", summary=False, resources_dir=tmp_path / "imgs")
    assert (tmp_path / "imgs" / "v" / "frame_00h00m00.000s.jpg").is_file()


def test_make_notes_video_without_audio(tmp_path, fake_transcribe):
    video = make_video(tmp_path / "v.mp4", [("black", 2)], with_audio=False)
    text = app.make_notes(video, tmp_path / "v.md", summary=False).read_text(encoding="utf-8")
    assert fake_transcribe == []
    assert "_(no speech)_" in text


def test_make_notes_adds_summary(tmp_path, fake_transcribe, monkeypatch):
    seen = {}

    def fake_summarize(text, model, url, hint):
        seen.update(text=text, model=model, hint=hint)
        return Summary("It was about slides.", ["Point one"], model)
    monkeypatch.setattr(app, "summarize", fake_summarize)
    video = make_video(tmp_path / "v.mp4", [("black", 2)])

    text = app.make_notes(video, tmp_path / "v.md", ollama_model="m1", hint="H") \
        .read_text(encoding="utf-8")

    assert seen == {"text": "First slide talk. Second slide talk.", "model": "m1", "hint": "H"}
    assert "## Summary" in text and "It was about slides." in text and "- Point one" in text


def test_make_notes_without_ollama_still_writes_notes(tmp_path, fake_transcribe, monkeypatch):
    def broken(*args, **kwargs):
        raise SummaryError("can't reach Ollama")
    monkeypatch.setattr(app, "summarize", broken)
    video = make_video(tmp_path / "v.mp4", [("black", 2)])
    text = app.make_notes(video, tmp_path / "v.md").read_text(encoding="utf-8")
    assert "## Summary" not in text and "First slide talk." in text


def test_make_notes_passes_mode_to_splitter(tmp_path, fake_transcribe):
    # a 6 % change: a new frame in presentation mode, ignored with camera mode's 40 %
    video = make_video(tmp_path / "v.mp4", [("black", 2), (f"black,{BOX}", 2)])
    slides = app.make_notes(video, tmp_path / "a.md", summary=False, mode="presentation")
    camera = app.make_notes(video, tmp_path / "b.md", summary=False, mode="camera")
    assert "\n# v\n\n2 frames" in slides.read_text(encoding="utf-8")
    assert "\n# v\n\n1 frames" in camera.read_text(encoding="utf-8")


def test_make_notes_missing_video(tmp_path):
    with pytest.raises(FileNotFoundError):
        app.make_notes(tmp_path / "nope.mp4", tmp_path / "n.md")


# ---------- speakers ----------

@pytest.fixture
def fake_diarize(monkeypatch):
    """Fake pyannote: speaker A talks for the first 2.5 s, then speaker B."""
    import importlib
    diarize = importlib.import_module("transcription.diarize")
    calls = []

    def fake_turns(audio, token, num_speakers=None):
        calls.append((audio.name, token, num_speakers))
        return [(0.0, 2.5, "SPEAKER_00"), (2.5, 6.0, "SPEAKER_01")]
    monkeypatch.setattr(diarize, "diarize_turns", fake_turns)
    monkeypatch.setattr(diarize, "find_token", lambda: "tok")
    return diarize, calls


def test_make_notes_with_speakers(tmp_path, fake_transcribe, fake_diarize):
    _, calls = fake_diarize
    video = make_video(tmp_path / "v.mp4", [("black", 2), ("white", 4)])
    text = app.make_notes(video, tmp_path / "v.md", summary=False, speakers=True,
                          num_speakers=2).read_text(encoding="utf-8")
    assert calls == [("audio.wav", "tok", 2)]
    assert "· 2 speakers" in text
    assert "**Speaker 1:** First slide talk." in text
    assert "**Speaker 2:** Second slide talk." in text


def test_make_notes_without_speakers_flag_does_not_diarize(tmp_path, fake_transcribe,
                                                          fake_diarize):
    _, calls = fake_diarize
    video = make_video(tmp_path / "v.mp4", [("black", 2)])
    text = app.make_notes(video, tmp_path / "v.md", summary=False).read_text(encoding="utf-8")
    assert calls == [] and "**Speaker" not in text


def test_speakers_failure_still_writes_notes(tmp_path, fake_transcribe, fake_diarize,
                                             monkeypatch, capsys):
    diarize, _ = fake_diarize

    def broken(*args, **kwargs):
        raise RuntimeError("speaker detection needs pyannote.audio")
    monkeypatch.setattr(diarize, "diarize_turns", broken)
    video = make_video(tmp_path / "v.mp4", [("black", 2)])
    text = app.make_notes(video, tmp_path / "v.md", summary=False, speakers=True) \
        .read_text(encoding="utf-8")
    assert "First slide talk." in text and "**Speaker" not in text
    assert "warning: no speaker labels" in capsys.readouterr().err


def test_speakers_without_token(tmp_path, fake_transcribe, fake_diarize, monkeypatch,
                                capsys):
    diarize, calls = fake_diarize
    monkeypatch.setattr(diarize, "find_token", lambda: None)
    video = make_video(tmp_path / "v.mp4", [("black", 2)])
    app.make_notes(video, tmp_path / "v.md", summary=False, speakers=True)
    assert calls == [] and "Hugging Face token" in capsys.readouterr().err


def test_main_speaker_options(tmp_path, monkeypatch):
    got = {}

    def fake_make_notes(video, output, **kwargs):
        got.update(kwargs)
        output = tmp_path / "w.md"
        output.write_text("x")
        return output
    monkeypatch.setattr(app, "make_notes", fake_make_notes)
    assert app.main(["v.mp4"]) == 0
    assert got["speakers"] is False and got["num_speakers"] is None
    assert app.main(["v.mp4", "--speakers"]) == 0
    assert got["speakers"] is True
    assert app.main(["v.mp4", "--num-speakers", "3"]) == 0   # implies --speakers
    assert got["speakers"] is True and got["num_speakers"] == 3


# ---------- transcribe_audio (GPU -> CPU fallback) ----------

@pytest.fixture
def fake_whisper(monkeypatch):
    """Fake WhisperModel; transcribing fails on cuda, like a missing cublas DLL."""
    devices = []

    class Model:
        def __init__(self, name, device, compute_type):
            devices.append((device, compute_type))
            self.device = device

    def fake_transcribe_one(model, batched_model, audio, lang, batched):
        if model.device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found")
        return audio.with_suffix(".txt")

    monkeypatch.setattr(faster_whisper, "WhisperModel", Model)
    monkeypatch.setattr(transcribe, "transcribe_one", fake_transcribe_one)
    monkeypatch.setattr(transcribe, "detect_device",
                        lambda d: ("cuda", "float16") if d in ("auto", "cuda") else ("cpu", "int8"))
    return devices


def test_transcribe_audio_falls_back_to_cpu(tmp_path, fake_whisper):
    out = app.transcribe_audio(tmp_path / "a.wav", "tiny", "auto", None, False)
    assert out == tmp_path / "a.txt"
    assert fake_whisper == [("cuda", "float16"), ("cpu", "int8")]


def test_transcribe_audio_no_fallback_when_gpu_was_asked_for(tmp_path, fake_whisper):
    with pytest.raises(RuntimeError, match="cublas"):
        app.transcribe_audio(tmp_path / "a.wav", "tiny", "cuda", None, False)
    assert fake_whisper == [("cuda", "float16")]


# ---------- app.main ----------

def test_main_saves_to_meeting_notes_folder_by_default(tmp_path, monkeypatch):
    got = {}

    def fake_make_notes(video, output, **kwargs):
        got.update(video=video, output=output, **kwargs)
        output = tmp_path / "written.md"
        output.write_text("x")
        return output
    monkeypatch.setattr(app, "make_notes", fake_make_notes)

    assert app.main(["data/raw/My talk.mp4", "--no-summary", "--mode", "camera"]) == 0
    root = Path(__file__).resolve().parents[1]
    assert got["output"] == root / "data" / "meeting_notes" / "My talk.md"
    assert got["summary"] is False and got["mode"] == "camera"
    assert got["resources_dir"] is None and got["embed_images"] is False


def test_default_folders_are_in_data():
    root = Path(__file__).resolve().parents[1]
    assert app.NOTES_DIR == root / "data" / "meeting_notes"
    assert app.DATA_DIR / "resources" == root / "data" / "resources"


def test_main_resources_and_embed_options(tmp_path, monkeypatch):
    got = {}

    def fake_make_notes(video, output, **kwargs):
        got.update(kwargs)
        output = tmp_path / "w.md"
        output.write_text("x")
        return output
    monkeypatch.setattr(app, "make_notes", fake_make_notes)
    assert app.main(["v.mp4", "--resources", str(tmp_path / "r"), "--embed-images"]) == 0
    assert got["resources_dir"] == tmp_path / "r" and got["embed_images"] is True


def test_main_output_option(tmp_path, monkeypatch):
    got = {}

    def fake_make_notes(video, output, **kwargs):
        got["output"] = output
        output.write_text("x")
        return output
    monkeypatch.setattr(app, "make_notes", fake_make_notes)
    assert app.main(["v.mp4", "-o", str(tmp_path / "n.md")]) == 0
    assert got["output"] == tmp_path / "n.md"


def test_main_missing_video_returns_error(tmp_path, capsys):
    assert app.main([str(tmp_path / "nope.mp4")]) == 1
    assert "error:" in capsys.readouterr().err


# ---------- other command-line entry points ----------

def test_video_splitter_cli(tmp_path, capsys):
    video = make_video(tmp_path / "v.mp4", [("black", 2), ("white", 2)])
    assert splitter_cli.main([str(video), "-o", str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out
    assert "presentation video" in printed and "2 frames" in printed
    assert (tmp_path / "out" / "frames" / "timestamps.csv").is_file()


def test_video_splitter_cli_missing_file(tmp_path, capsys):
    assert splitter_cli.main([str(tmp_path / "nope.mp4")]) == 1
    assert "error:" in capsys.readouterr().err


def test_meeting_notes_cli(tmp_path):
    split = tmp_path / "talk_split"
    (split / "frames").mkdir(parents=True)
    (split / "frames" / "f.jpg").write_bytes(b"jpg")
    (split / "frames" / "timestamps.csv").write_text(
        "frame,seconds,timestamp,file,reason,change\n1,0.000,x,f.jpg,first,0\n",
        encoding="utf-8")
    (split / "audio.txt").write_text("[   0.00 ->    1.00] Hi.\n", encoding="utf-8")
    assert meeting_notes.main([str(split), "--no-summary"]) == 0
    assert "Hi." in (split / "notes.md").read_text(encoding="utf-8")


def test_meeting_notes_cli_missing_folder(tmp_path, capsys):
    assert meeting_notes.main([str(tmp_path / "nope"), "--no-summary"]) == 1
    assert "error:" in capsys.readouterr().err


# ---------- summarize._chunks ----------

def test_chunks():
    assert _chunks("a b c d e", 2) == ["a b", "c d", "e"]
    assert _chunks("a  b\n c", 10) == ["a b c"]
    assert _chunks("   ", 5) == [""]


# ---------- audio-only files (e.g. a podcast MP3) ----------

def make_audio(path: Path, seconds: int = 3) -> Path:
    from video_splitter.splitter import find_ffmpeg
    import subprocess
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", "-c:a", "aac", str(path)],
                   check=True)
    return path


def test_has_video(tmp_path):
    from video_splitter.splitter import find_ffmpeg, has_video
    assert has_video(make_video(tmp_path / "v.mp4", [("black", 1)]), find_ffmpeg())
    assert not has_video(make_audio(tmp_path / "a.m4a"), find_ffmpeg())


def test_audio_only_file_gives_notes_in_parts(tmp_path, monkeypatch, resources_in_tmp):
    def fake(audio, model_name, device, lang, batched):
        out = audio.with_suffix(".txt")
        out.write_text("[   0.00 ->    5.00] Welcome to the show.\n"
                       "[ 290.00 ->  320.00] Crossing the five minute mark.\n"
                       "[ 650.00 ->  655.00] Late in the episode.\n", encoding="utf-8")
        return out
    monkeypatch.setattr(app, "transcribe_audio", fake)
    podcast = make_audio(tmp_path / "Podcast #1.m4a")

    md = app.make_notes(podcast, tmp_path / "notes" / "p.md", summary=False)

    text = md.read_text(encoding="utf-8")
    assert "audio only · 3 parts · 00:10:55 long" in text
    assert "![" not in text and not resources_in_tmp.exists()     # no frames anywhere
    assert "## 1. 00:00:00 – 00:05:00" in text and "## 3. 00:10:00 – 00:10:55" in text
    one, two = text.split("## 2.")[0], text.split("## 2.")[1].split("## 3.")[0]
    assert "Welcome to the show." in one
    assert "Crossing the five minute mark." in two                 # mostly after 5:00
    assert text.startswith("---\nfile_size: ")                     # fingerprint too


def test_build_notes_parts_without_frames(tmp_path):
    split = tmp_path / "s"
    split.mkdir()
    (split / "audio.txt").write_text("[   0.00 ->    1.00] Hi.\n", encoding="utf-8")
    text = meeting_notes.build_notes(split, part_seconds=300).read_text(encoding="utf-8")
    assert "audio only · 1 parts" in text and "Hi." in text and "![" not in text

"""Tests for the local website (src/website), videohash and the video id in the notes."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import videohash  # noqa: E402
from notes.meeting_notes import (VideoInfo, build_notes, embed_local_images,  # noqa: E402
                                 find_notes, read_video_id)
from test_meeting_notes import TRANSCRIPT, make_split_dir  # noqa: E402
from test_splitter import make_video  # noqa: E402
from video_splitter.splitter import find_ffmpeg  # noqa: E402
from website import server  # noqa: E402


# ---------- videohash ----------

def with_title(src: Path, dst: Path, title: str) -> Path:
    """Copy of the video with other metadata, same video/audio data."""
    subprocess.run([find_ffmpeg(), "-v", "error", "-y", "-i", str(src), "-c", "copy",
                    "-metadata", f"title={title}", str(dst)], check=True)
    return dst


def test_content_hash_ignores_metadata(tmp_path):
    video = make_video(tmp_path / "a.mp4", [("black", 1), ("white", 1)])
    other = with_title(video, tmp_path / "b.mp4", "A new title")
    assert video.read_bytes() != other.read_bytes()
    assert videohash.hash_video_innehall(video) == videohash.hash_video_innehall(other)
    assert videohash.hash_video(video) != videohash.hash_video(other)   # whole file differs


def test_different_videos_get_different_ids(tmp_path):
    a = make_video(tmp_path / "a.mp4", [("black", 1)])
    b = make_video(tmp_path / "b.mp4", [("white", 1)])
    assert videohash.video_id(a)[0] != videohash.video_id(b)[0]


def test_ids_fit_sqlite_integer(tmp_path):
    vid, method = videohash.video_id(make_video(tmp_path / "a.mp4", [("black", 1)]))
    assert method == "content"
    assert -2**63 <= vid < 2**63


def test_video_id_falls_back_to_file_hash(tmp_path, monkeypatch):
    def no_ffmpeg():
        raise RuntimeError("ffmpeg not found")
    monkeypatch.setattr(videohash, "_ffmpeg", no_ffmpeg)
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    assert videohash.video_id(video) == (videohash.hash_video(video), "file")


# ---------- video id in the notes ----------

def test_notes_start_with_fingerprint_only(tmp_path):
    split = make_split_dir(tmp_path, [0], TRANSCRIPT)
    info = VideoInfo(1234, "ab" * 32, "sha256-size-first-last-1MiB", "cd" * 32, "sha256")
    md = build_notes(split, video=info)
    text = md.read_text(encoding="utf-8")
    assert text.startswith(
        "---\n"
        "file_size: 1234\n"
        f"quick_hash: {'ab' * 32}\nquick_hash_method: sha256-size-first-last-1MiB\n"
        f"full_hash: {'cd' * 32}\nfull_hash_algorithm: sha256\n"
        "---\n\n# My talk\n\n---\n")
    assert "video_id" not in text and "Video: `" not in text and "frames ·" not in text


def test_keywords_go_in_front_matter(tmp_path):
    from notes.meeting_notes import read_keywords
    split = make_split_dir(tmp_path, [0], TRANSCRIPT)
    info = VideoInfo(1, "ab" * 32, "m", "cd" * 32, "sha256")
    seen = []
    md = build_notes(split, video=info,
                     keyworder=lambda text: seen.append(text) or ["GraalVM", "native image"])
    text = md.read_text(encoding="utf-8")
    assert 'full_hash_algorithm: sha256\nkeywords: ["GraalVM", "native image"]\n---' in text
    assert read_keywords(md) == ["GraalVM", "native image"]
    assert seen and seen[0].startswith("Hello and welcome.")   # gets the transcript


def test_no_keywords_no_line(tmp_path):
    from notes.meeting_notes import read_keywords
    split = make_split_dir(tmp_path, [0], TRANSCRIPT)
    md = build_notes(split, keyworder=lambda text: [])
    assert "keywords" not in md.read_text(encoding="utf-8") and read_keywords(md) == []


def test_read_video_id_without_front_matter(tmp_path):
    split = make_split_dir(tmp_path, [0], TRANSCRIPT)
    assert read_video_id(build_notes(split)) is None
    assert read_video_id(tmp_path / "missing.md") is None


def test_find_notes(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("---\nvideo_id: 1\n---\n# a\n", encoding="utf-8")
    (notes / "b.md").write_text("---\nvideo_id: -7\n---\n# b\n", encoding="utf-8")
    (notes / "c.md").write_text("# no front matter\n", encoding="utf-8")
    assert find_notes(notes, -7) == notes / "b.md"
    assert find_notes(notes, 99) is None


def test_embed_local_images(tmp_path):
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "f 1.jpg").write_bytes(b"jpg")
    md = ("![a](res/f%201.jpg)\n![b](res/missing.jpg)\n"
          "![c](https://example.com/x.jpg)\n![d](data:image/png;base64,AAA=)\n")
    out = embed_local_images(md, tmp_path)
    assert out.splitlines() == [
        "![a](data:image/jpeg;base64,anBn)",          # b"jpg"
        "![b](res/missing.jpg)",
        "![c](https://example.com/x.jpg)",
        "![d](data:image/png;base64,AAA=)"]


# ---------- website ----------

@pytest.fixture
def site(tmp_path, monkeypatch):
    """Flask test client; notes go to tmp_path, the pipeline is faked."""
    notes = tmp_path / "meeting_notes"
    resources = tmp_path / "resources"
    monkeypatch.setattr(server.pipeline, "NOTES_DIR", notes)
    monkeypatch.setattr(server.pipeline, "RESOURCES_DIR", resources)
    calls = []
    # set "works" to False to simulate a missing HF token, "ollama" for Ollama down
    diarization = {"works": True, "ollama": True}

    def fake_make_notes(video, output, **kwargs):
        calls.append((video.name, output, kwargs))
        fp = videohash.fingerprint(video)
        frame_dir = resources / video.stem
        frame_dir.mkdir(parents=True, exist_ok=True)
        (frame_dir / "frame_1.jpg").write_bytes(b"jpg")
        output.parent.mkdir(parents=True, exist_ok=True)
        n = (kwargs.get("num_speakers") or 2) if kwargs.get("speakers") and \
            diarization["works"] else 0
        front = (f"file_size: {fp.file_size}\nquick_hash: {fp.quick_hash}\n"
                 f"quick_hash_method: {fp.quick_hash_method}\nfull_hash: {fp.full_hash}\n"
                 f"full_hash_algorithm: {fp.full_hash_algorithm}\n")
        if kwargs.get("keywords") and diarization["ollama"]:
            front += 'keywords: ["test"]\n'
        if n:
            front += f"speakers: {n}\n"
        summary = "## Summary\n\nIt was a test.\n\n" \
            if kwargs.get("summary", True) and diarization["ollama"] else ""
        output.write_text(f"---\n{front}---\n\n# {video.stem}\n\n{summary}"
                          f"![Frame](../resources/{video.stem}/frame_1.jpg)\n",
                          encoding="utf-8")
        return output
    monkeypatch.setattr(server.pipeline, "make_notes", fake_make_notes)
    monkeypatch.setitem(server.INSTALLNINGAR, "speakers", False)
    server.app.config["TESTING"] = True
    client = server.app.test_client()
    client.diarization = diarization
    return client, calls, notes


def upload(client, path: Path, name: str | None = None, **form):
    return client.post("/api/compress", data={
        "file": (io.BytesIO(path.read_bytes()), name or path.name), **form},
        content_type="multipart/form-data")


def test_upload_video_returns_notes_with_embedded_images(site, tmp_path):
    client, calls, notes = site
    video = make_video(tmp_path / "My talk.mp4", [("black", 1)])

    resp = upload(client, video)

    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"].endswith("My_talk.md")
    assert resp.headers["X-File-Id"] == videohash.full_hash(video)
    assert resp.headers["X-Cached"] == "0"
    body = resp.data.decode("utf-8")
    assert "![Frame](data:image/jpeg;base64,anBn)" in body       # self-contained download
    saved = (notes / "My_talk.md").read_text(encoding="utf-8")
    assert "../resources/My_talk/frame_1.jpg" in saved             # server copy links
    assert len(calls) == 1 and calls[0][2]["speakers"] is False


def test_same_file_again_uses_saved_notes(site, tmp_path):
    client, calls, _ = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video)

    resp = upload(client, video, name="renamed copy.mp4")           # same bytes

    assert resp.status_code == 200 and resp.headers["X-Cached"] == "1"
    assert resp.headers["Content-Disposition"].endswith("a.md")
    assert len(calls) == 1                                           # not processed again


def test_changed_metadata_counts_as_another_file(site, tmp_path):
    client, calls, _ = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video)
    retitled = with_title(video, tmp_path / "b.mp4", "other title")  # other bytes
    assert upload(client, retitled).headers["X-Cached"] == "0"
    assert len(calls) == 2


def test_other_video_with_same_name_gets_own_notes(site, tmp_path):
    client, calls, notes = site
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    first = make_video(tmp_path / "x" / "talk.mp4", [("black", 1)])
    second = make_video(tmp_path / "y" / "talk.mp4", [("white", 1)])
    upload(client, first)
    resp = upload(client, second)
    expected = f"talk_{videohash.full_hash(second)[:8]}.md"
    assert resp.headers["Content-Disposition"].endswith(expected)
    assert sorted(p.name for p in notes.iterdir()) == sorted(["talk.md", expected])


def test_non_video_is_rejected(site, tmp_path):
    client, calls, _ = site
    doc = tmp_path / "notes.pdf"
    doc.write_bytes(b"%PDF-1.4")
    resp = upload(client, doc)
    assert resp.status_code == 415 and "videofiler" in resp.get_json()["detail"]
    assert calls == []


def test_missing_and_empty_file(site, tmp_path):
    client, _, _ = site
    assert client.post("/api/compress", data={}).status_code == 400
    empty = tmp_path / "e.mp4"
    empty.write_bytes(b"")
    assert upload(client, empty).status_code == 400


def test_pipeline_error_is_reported(site, tmp_path, monkeypatch):
    client, _, _ = site

    def broken(*args, **kwargs):
        raise RuntimeError("GPU on fire")
    monkeypatch.setattr(server.pipeline, "make_notes", broken)
    resp = upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]))
    assert resp.status_code == 500 and "GPU on fire" in resp.get_json()["detail"]


def test_uploaded_video_is_not_kept(site, tmp_path, monkeypatch):
    client, calls, _ = site
    seen = []
    real = server.komprimera

    def spy(path, *args):
        seen.append(path.parent)
        return real(path, *args)
    monkeypatch.setattr(server, "komprimera", spy)
    upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]))
    assert seen and not seen[0].exists()


def test_serves_the_page(site):
    client, _, _ = site
    page = client.get("/")
    assert page.status_code == 200 and b"function.js" in page.data
    assert client.get("/function.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/server.py").status_code == 404     # no source files served


# ---------- choice: who is speaking ----------

def test_speaker_checkbox_is_passed_to_pipeline(site, tmp_path):
    client, calls, notes = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    resp = upload(client, video, speakers="1")
    assert resp.status_code == 200 and "X-Warning" not in resp.headers
    assert calls[0][2]["speakers"] is True and calls[0][2]["num_speakers"] is None
    assert "speakers: 2" in (notes / "a.md").read_text(encoding="utf-8")


def test_speakers_off_by_default(site, tmp_path):
    client, calls, _ = site
    upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]))
    assert calls[0][2]["speakers"] is False


def test_number_of_speakers(site, tmp_path):
    client, calls, _ = site
    upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]), num_speakers="3")
    assert calls[0][2]["speakers"] is True and calls[0][2]["num_speakers"] == 3


@pytest.mark.parametrize("bad", ["0", "21", "two", "-1"])
def test_bad_number_of_speakers(site, tmp_path, bad):
    client, calls, _ = site
    resp = upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]),
                  speakers="1", num_speakers=bad)
    assert resp.status_code == 400 and "Antal talare" in resp.get_json()["detail"]
    assert calls == []


def test_saved_notes_without_speakers_are_redone_with_speakers(site, tmp_path):
    client, calls, notes = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video)                                     # first: no speakers
    resp = upload(client, video, speakers="1")                # now with speakers

    assert resp.headers["X-Cached"] == "0"
    assert len(calls) == 2 and calls[1][2]["speakers"] is True
    assert calls[1][1] == notes / "a.md"                      # same notes file, updated
    assert sorted(p.name for p in notes.iterdir()) == ["a.md"]
    assert "speakers: 2" in (notes / "a.md").read_text(encoding="utf-8")


def test_saved_notes_with_speakers_are_reused(site, tmp_path):
    client, calls, _ = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video, speakers="1")
    assert upload(client, video, speakers="1").headers["X-Cached"] == "1"
    assert upload(client, video).headers["X-Cached"] == "1"   # labels don't hurt
    assert len(calls) == 1


def test_other_number_of_speakers_is_redone(site, tmp_path):
    client, calls, _ = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video, speakers="1")                        # found 2
    assert upload(client, video, num_speakers="2").headers["X-Cached"] == "1"
    assert upload(client, video, num_speakers="4").headers["X-Cached"] == "0"
    assert len(calls) == 2 and calls[1][2]["num_speakers"] == 4


def test_warning_when_speakers_could_not_be_found(site, tmp_path):
    from urllib.parse import unquote
    client, _, _ = site
    client.diarization["works"] = False
    resp = upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]), speakers="1")
    assert resp.status_code == 200                             # notes still come back
    warning = unquote(resp.headers["X-Warning"])
    assert "vem som pratar" in warning and ".hf_token" in warning
    assert "X-Warning" in resp.headers["Access-Control-Expose-Headers"]


def test_page_has_speaker_choice(site):
    client, _, _ = site
    page = client.get("/").data.decode("utf-8")
    assert 'id="speakersInput"' in page and 'id="numSpeakersInput"' in page


def test_real_notes_record_speaker_count(tmp_path):
    from notes.meeting_notes import read_speaker_count
    from test_meeting_notes import DIARIZED
    split = make_split_dir(tmp_path, [0], DIARIZED)
    with_labels = build_notes(split, speakers=True, video=VideoInfo("v.mp4", 5))
    assert read_speaker_count(with_labels) == 2
    without = build_notes(split, output=tmp_path / "plain.md", video=VideoInfo("v.mp4", 5))
    assert read_speaker_count(without) == 0
    assert "speakers:" not in without.read_text(encoding="utf-8")



# ---------- choice: summary and keywords ----------

def test_summary_and_keywords_choices_are_passed(site, tmp_path):
    client, calls, _ = site
    upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]), summary="0", keywords="1")
    assert calls[0][2]["summary"] is False and calls[0][2]["keywords"] is True


def test_defaults_summary_on_keywords_off(site, tmp_path):
    client, calls, _ = site
    upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]))
    assert calls[0][2]["summary"] is True and calls[0][2]["keywords"] is False


def test_asking_for_keywords_redoes_notes_without_them(site, tmp_path):
    client, calls, notes = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video)                                    # no keywords
    assert upload(client, video).headers["X-Cached"] == "1"
    resp = upload(client, video, keywords="1")
    assert resp.headers["X-Cached"] == "0" and len(calls) == 2
    assert 'keywords: ["test"]' in (notes / "a.md").read_text(encoding="utf-8")


def test_notes_without_summary_are_redone_when_summary_wanted(site, tmp_path):
    client, calls, _ = site
    video = make_video(tmp_path / "a.mp4", [("black", 1)])
    upload(client, video, summary="0")
    assert upload(client, video, summary="0").headers["X-Cached"] == "1"
    assert upload(client, video, summary="1").headers["X-Cached"] == "0"
    assert len(calls) == 2


def test_warning_when_ollama_is_down(site, tmp_path):
    from urllib.parse import unquote
    client, _, _ = site
    client.diarization["ollama"] = False
    resp = upload(client, make_video(tmp_path / "a.mp4", [("black", 1)]), keywords="1")
    warning = unquote(resp.headers["X-Warning"])
    assert resp.status_code == 200
    assert "Ingen sammanfattning" in warning and "Inga sökord" in warning


def test_page_has_summary_and_keyword_choices(site):
    client, _, _ = site
    page = client.get("/").data.decode("utf-8")
    assert 'id="summaryInput" checked' in page and 'id="keywordsInput"' in page

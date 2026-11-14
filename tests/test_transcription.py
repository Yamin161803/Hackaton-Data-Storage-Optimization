import importlib
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notes.meeting_notes import read_transcript  # noqa: E402
from transcription import transcribe  # noqa: E402
from transcription.transcribe import add_nvidia_dll_dirs, detect_device, transcribe_one  # noqa: E402


# ---------- fakes for faster-whisper ----------

class Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class Info:
    language, language_probability, duration = "en", 0.99, 12.0


class FakeModel:
    def __init__(self, segments):
        self.segments = segments
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return iter(self.segments), Info()


def fake_ctranslate2(monkeypatch, count=None, error=None):
    def get_cuda_device_count():
        if error:
            raise error
        return count
    module = types.SimpleNamespace(get_cuda_device_count=get_cuda_device_count)
    monkeypatch.setitem(sys.modules, "ctranslate2", module)


# ---------- transcribe.detect_device ----------

def test_detect_device_explicit():
    assert detect_device("cuda") == ("cuda", "float16")
    assert detect_device("cpu") == ("cpu", "int8")


def test_detect_device_auto_uses_gpu_when_present(monkeypatch):
    fake_ctranslate2(monkeypatch, count=1)
    assert detect_device("auto") == ("cuda", "float16")


def test_detect_device_auto_falls_back_to_cpu(monkeypatch):
    fake_ctranslate2(monkeypatch, count=0)
    assert detect_device("auto") == ("cpu", "int8")
    fake_ctranslate2(monkeypatch, error=RuntimeError("no driver"))
    assert detect_device("auto") == ("cpu", "int8")


# ---------- transcribe.transcribe_one ----------

def test_transcribe_one_writes_transcript_that_meeting_notes_can_read(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"")
    model = FakeModel([Seg(0.0, 2.5, "  Hello there. "), Seg(2.5, 7.25, "Second line.")])

    out = transcribe_one(model, None, audio, "en", use_batched=False)

    assert out == tmp_path / "audio.txt"
    assert out.read_text(encoding="utf-8") == (
        "[   0.00 ->    2.50] Hello there.\n"
        "[   2.50 ->    7.25] Second line.\n")
    assert [(s.start, s.end, s.text) for s in read_transcript(out)] == [
        (0.0, 2.5, "Hello there."), (2.5, 7.25, "Second line.")]
    assert model.calls == [(str(audio), {"language": "en", "vad_filter": True})]


def test_transcribe_one_uses_batched_model_when_asked(tmp_path):
    audio = tmp_path / "a.wav"
    model, batched = FakeModel([]), FakeModel([Seg(0, 1, "hi")])
    transcribe_one(model, batched, audio, None, use_batched=True)
    assert model.calls == []
    assert batched.calls[0][1]["batch_size"] == 16


def test_transcribe_one_without_batched_model_uses_normal_model(tmp_path):
    model = FakeModel([Seg(0, 1, "hi")])
    transcribe_one(model, None, tmp_path / "a.wav", None, use_batched=True)
    assert len(model.calls) == 1


# ---------- transcribe.add_nvidia_dll_dirs ----------

@pytest.mark.skipif(os.name != "nt", reason="DLL search path is Windows only")
def test_add_nvidia_dll_dirs_registers_every_bin_folder(tmp_path, monkeypatch):
    for lib in ("cublas", "cudnn"):
        (tmp_path / lib / "bin").mkdir(parents=True)
    (tmp_path / "no_bin_here").mkdir()
    monkeypatch.setitem(sys.modules, "nvidia", types.SimpleNamespace(__path__=[str(tmp_path)]))
    added = []
    monkeypatch.setattr(transcribe.os, "add_dll_directory", added.append)
    monkeypatch.setenv("PATH", "original")

    add_nvidia_dll_dirs()

    expected = {str(tmp_path / "cublas" / "bin"), str(tmp_path / "cudnn" / "bin")}
    assert set(added) == expected
    parts = os.environ["PATH"].split(os.pathsep)
    assert set(parts[:2]) == expected and parts[-1] == "original"


@pytest.mark.skipif(os.name != "nt", reason="DLL search path is Windows only")
def test_add_nvidia_dll_dirs_without_nvidia_packages(monkeypatch):
    monkeypatch.setitem(sys.modules, "nvidia", None)  # makes "import nvidia" fail
    monkeypatch.setattr(transcribe.os, "add_dll_directory",
                        lambda p: pytest.fail("should not be called"))
    add_nvidia_dll_dirs()


@pytest.mark.skipif(os.name == "nt", reason="non-Windows behaviour")
def test_add_nvidia_dll_dirs_does_nothing_off_windows(monkeypatch):
    before = os.environ.get("PATH")
    add_nvidia_dll_dirs()
    assert os.environ.get("PATH") == before


# ---------- diarize ----------

@pytest.fixture
def diarize():
    # diarize.py imports torch / pyannote / soundfile only inside diarize_turns, so the
    # module itself imports without them.
    return importlib.import_module("transcription.diarize")


def test_assign_speaker_picks_most_overlap(diarize):
    turns = [(0.0, 4.0, "A"), (4.0, 10.0, "B"), (10.0, 12.0, "A")]
    assert diarize.assign_speaker(1.0, 3.0, turns) == "A"
    assert diarize.assign_speaker(3.0, 8.0, turns) == "B"      # 1 s A, 4 s B
    assert diarize.assign_speaker(9.0, 12.0, turns) == "A"     # 1 s B, 2 s A


def test_assign_speaker_unknown_without_overlap(diarize):
    assert diarize.assign_speaker(20.0, 25.0, [(0.0, 4.0, "A")]) == "UNKNOWN"
    assert diarize.assign_speaker(1.0, 2.0, []) == "UNKNOWN"


def test_read_token_prefers_env(diarize, monkeypatch, tmp_path):
    monkeypatch.setattr(diarize, "HERE", tmp_path)
    (tmp_path / ".hf_token").write_text("from-file\n")
    monkeypatch.setenv("HF_TOKEN", " from-env \n")
    assert diarize.read_token() == "from-env"


def test_read_token_from_file(diarize, monkeypatch, tmp_path):
    monkeypatch.setattr(diarize, "HERE", tmp_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    (tmp_path / ".hf_token").write_text("  hf_abc  \n")
    assert diarize.read_token() == "hf_abc"


def test_read_token_missing_exits(diarize, monkeypatch, tmp_path):
    monkeypatch.setattr(diarize, "HERE", tmp_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        diarize.read_token()


def test_token_file_is_looked_up_in_project_root(diarize):
    assert diarize.HERE == Path(__file__).resolve().parents[1]


def test_fw_device(diarize, monkeypatch):
    fake_ctranslate2(monkeypatch, count=2)
    assert diarize.fw_device() == ("cuda", "float16")
    fake_ctranslate2(monkeypatch, count=0)
    assert diarize.fw_device() == ("cpu", "int8")
    fake_ctranslate2(monkeypatch, error=OSError("broken"))
    assert diarize.fw_device() == ("cpu", "int8")


def test_find_token(diarize, monkeypatch, tmp_path):
    monkeypatch.setattr(diarize, "HERE", tmp_path)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert diarize.find_token() is None
    (tmp_path / ".hf_token").write_text("  \n")
    assert diarize.find_token() is None          # empty file
    (tmp_path / ".hf_token").write_text("hf_x\n")
    assert diarize.find_token() == "hf_x"
    monkeypatch.setenv("HF_TOKEN", "   ")          # blank env var is ignored
    assert diarize.find_token() == "hf_x"


def test_diarize_turns_without_pyannote_says_what_to_install(diarize, monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pyannote.audio", None)   # "import" fails
    with pytest.raises(RuntimeError, match="pip install pyannote.audio"):
        diarize.diarize_turns(tmp_path / "a.wav", "token")


def _fake_pyannote(monkeypatch, cuda: bool = False, gpu_fails: bool = False):
    """Fake torch / soundfile / pyannote. Returns the dict of calls the pipeline gets."""
    np = pytest.importorskip("numpy")
    calls = {"devices": []}

    class Turn:
        def __init__(self, start, end):
            self.start, self.end = start, end

    class Annotation:
        def itertracks(self, yield_label):
            yield Turn(0.0, 2.0), "t1", "SPEAKER_00"

    class FakePipeline:
        device = None

        @staticmethod
        def from_pretrained(name, token):
            return FakePipeline()

        def to(self, device):
            self.device = device
            calls["devices"].append(device)

        def __call__(self, audio, **kwargs):
            if gpu_fails and self.device == "cuda":
                raise RuntimeError("CUDA out of memory. Tried to allocate 2 GiB")
            return types.SimpleNamespace(speaker_diarization=Annotation())

    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: cuda, empty_cache=lambda: None),
        device=lambda d: d, from_numpy=lambda a: a)
    fake_sf = types.SimpleNamespace(
        read=lambda path, dtype, always_2d: (np.zeros((16000, 1), dtype="float32"), 16000))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", types.SimpleNamespace(Pipeline=FakePipeline))
    return calls


def test_diarize_runs_on_gpu_when_pytorch_can(diarize, monkeypatch, tmp_path):
    calls = _fake_pyannote(monkeypatch, cuda=True)
    seen = []
    turns = diarize.diarize_turns(tmp_path / "a.wav", "tok",
                                  on_device=lambda d, hint: seen.append((d, hint)))
    assert turns == [(0.0, 2.0, "SPEAKER_00")]
    assert calls["devices"] == ["cuda"] and seen == [("cuda", None)]


def test_diarize_falls_back_to_cpu_when_the_gpu_fails(diarize, monkeypatch, tmp_path):
    calls = _fake_pyannote(monkeypatch, cuda=True, gpu_fails=True)
    seen = []
    turns = diarize.diarize_turns(tmp_path / "a.wav", "tok",
                                  on_device=lambda d, hint: seen.append((d, hint)))
    assert turns == [(0.0, 2.0, "SPEAKER_00")]
    assert calls["devices"] == ["cuda", "cpu"]
    assert seen[-1][0] == "cpu" and "out of memory" in seen[-1][1]


def test_diarize_forced_gpu_does_not_fall_back(diarize, monkeypatch, tmp_path):
    _fake_pyannote(monkeypatch, cuda=True, gpu_fails=True)
    with pytest.raises(RuntimeError, match="out of memory"):
        diarize.diarize_turns(tmp_path / "a.wav", "tok", device="cuda")


def test_diarize_forced_cpu(diarize, monkeypatch, tmp_path):
    calls = _fake_pyannote(monkeypatch, cuda=True)
    diarize.diarize_turns(tmp_path / "a.wav", "tok", device="cpu")
    assert calls["devices"] == ["cpu"]


def test_diarize_hint_when_gpu_present_but_pytorch_is_cpu_only(diarize, monkeypatch, tmp_path):
    _fake_pyannote(monkeypatch, cuda=False)
    monkeypatch.setattr(diarize, "nvidia_gpu_present", lambda: True)
    seen = []
    diarize.diarize_turns(tmp_path / "a.wav", "tok", on_device=lambda d, h: seen.append((d, h)))
    assert seen[0][0] == "cpu" and "CPU-only" in seen[0][1] and "cu126" in seen[0][1]
    monkeypatch.setattr(diarize, "nvidia_gpu_present", lambda: False)
    seen.clear()
    diarize.diarize_turns(tmp_path / "a.wav", "tok", on_device=lambda d, h: seen.append((d, h)))
    assert seen == [("cpu", None)]                    # no GPU at all: nothing to fix


def test_diarize_turns_with_fake_pipeline(diarize, monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    calls = {}

    class Turn:
        def __init__(self, start, end):
            self.start, self.end = start, end

    class Annotation:
        def itertracks(self, yield_label):
            yield Turn(0.0, 2.0), "t1", "SPEAKER_00"
            yield Turn(2.0, 5.0), "t2", "SPEAKER_01"

    class FakePipeline:
        @staticmethod
        def from_pretrained(name, token):
            calls["load"] = (name, token)
            return FakePipeline()

        def to(self, device):
            calls["device"] = device

        def __call__(self, audio, **kwargs):
            calls["audio"] = (tuple(audio["waveform"].shape), audio["sample_rate"])
            calls["kwargs"] = kwargs
            return types.SimpleNamespace(speaker_diarization=Annotation())

    fake_torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False),
        device=lambda d: d,
        from_numpy=lambda a: a)
    fake_sf = types.SimpleNamespace(
        read=lambda path, dtype, always_2d: (np.zeros((16000, 2), dtype="float32"), 16000))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio",
                        types.SimpleNamespace(Pipeline=FakePipeline))

    turns = diarize.diarize_turns(tmp_path / "a.wav", "tok", num_speakers=2)

    assert turns == [(0.0, 2.0, "SPEAKER_00"), (2.0, 5.0, "SPEAKER_01")]
    assert calls["load"] == (diarize.DIARIZATION_MODEL, "tok")
    assert calls["audio"] == ((2, 16000), 16000)      # (channels, samples)
    assert calls["kwargs"] == {"num_speakers": 2}
    assert calls["device"] == "cpu"                    # no GPU -> runs on the CPU

"""Summarize a meeting transcript with a local model through Ollama (http://localhost:11434).

Only the Python standard library is used. The model is asked for JSON:
    {"overview": "3-5 sentences", "key_points": ["...", ...]}

Long transcripts are split into chunks; each chunk is summarized, then the partial
summaries are combined into one.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_MODEL = "llama3.2"
DEFAULT_URL = "http://localhost:11434"
CHUNK_WORDS = 3000      # ~4k tokens per request, fits the 8k context below
_NUM_CTX = 8192

_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "key_points"],
}

_SYSTEM = (
    "You summarize meeting transcripts. The transcript comes from automatic speech "
    "recognition and may contain misheard words; silently correct obvious mishearings of "
    "names and technical terms. Only state things that are said in the transcript. "
    "Reply in JSON: overview = 3-5 sentences on what the meeting was about; "
    "key_points = 5-10 short bullet points with the main points, findings and conclusions."
)


@dataclass
class Summary:
    overview: str
    key_points: list[str]
    model: str


class SummaryError(RuntimeError):
    pass


def _chat(messages: list[dict], model: str, url: str, timeout: float) -> dict:
    body = {"model": model, "stream": False, "format": _SCHEMA, "messages": messages,
            "options": {"num_ctx": _NUM_CTX, "temperature": 0.2}}
    req = urllib.request.Request(f"{url.rstrip('/')}/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SummaryError(f"Ollama has no usable model '{model}'. "
                               f"Run: ollama pull {model}") from exc
        raise SummaryError(f"Ollama returned HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise SummaryError(f"can't reach Ollama at {url} ({exc}). Is Ollama running?") from exc

    content = reply.get("message", {}).get("content", "")
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()  # reasoning models
    try:
        data = json.loads(content)
        overview = str(data["overview"]).strip()
        points = [str(p).strip() for p in data["key_points"] if str(p).strip()]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SummaryError(f"model did not return the expected JSON: {content[:300]!r}") from exc
    return {"overview": overview, "key_points": points}


def _chunks(text: str, size: int) -> list[str]:
    words = text.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)] or [""]


def summarize(transcript: str, model: str = DEFAULT_MODEL, url: str = DEFAULT_URL,
              hint: str | None = None, timeout: float = 900) -> Summary:
    """transcript: plain text of the meeting. hint: optional context, e.g. correct spellings."""
    if not transcript.strip():
        raise SummaryError("transcript is empty")
    system = _SYSTEM + (f"\nContext from the user (use it for correct names/terms): {hint}"
                        if hint else "")

    parts = _chunks(transcript, CHUNK_WORDS)
    if len(parts) == 1:
        result = _chat([{"role": "system", "content": system},
                        {"role": "user", "content": "Transcript:\n\n" + parts[0]}],
                       model, url, timeout)
    else:
        partials = []
        for i, part in enumerate(parts, start=1):
            partials.append(_chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": f"Transcript, part {i} of {len(parts)}:\n\n{part}"}],
                model, url, timeout))
        combined = "\n\n".join(
            f"Part {i}:\n{p['overview']}\n" + "\n".join(f"- {k}" for k in p["key_points"])
            for i, p in enumerate(partials, start=1))
        result = _chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": "These are summaries of consecutive parts of one "
                                         "meeting. Combine them into one summary of the whole "
                                         "meeting:\n\n" + combined}],
            model, url, timeout)
    return Summary(result["overview"], result["key_points"], model)

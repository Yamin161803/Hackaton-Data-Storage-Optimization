"""Summarize a meeting transcript with a local model through Ollama (http://localhost:11434).

Only the Python standard library is used. The model is asked for JSON:
    summarize()        -> {"overview": "3-5 sentences", "key_points": ["...", ...]}
    extract_keywords() -> {"keywords": ["graalvm", "native image", ...]}

Long transcripts are split into chunks; each chunk is handled on its own, then the
partial results are combined into one.
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


_KEYWORD_SCHEMA = {
    "type": "object",
    "properties": {"keywords": {"type": "array", "items": {"type": "string"}}},
    "required": ["keywords"],
}

_KEYWORD_SYSTEM = (
    "You pick search keywords for a meeting transcript, so the notes can be found later. "
    "The transcript comes from automatic speech recognition; silently correct obvious "
    "mishearings of names and technical terms. Choose the specific topics, technologies, "
    "products, companies, people, places and key concepts that are discussed. Each keyword "
    "is 1-3 words, lowercase except names and acronyms. No generic words such as "
    "'meeting', 'discussion', 'presentation' or 'question'. "
    "Reply in JSON: keywords = 5-10 keywords, most important first."
)
MAX_KEYWORDS = 15


@dataclass
class Summary:
    overview: str
    key_points: list[str]
    model: str


class SummaryError(RuntimeError):
    pass


def _ask(messages: list[dict], model: str, url: str, timeout: float,
         schema: dict) -> dict:
    """One Ollama chat call that must answer JSON matching `schema`; returns the JSON."""
    body = {"model": model, "stream": False, "format": schema, "messages": messages,
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
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise SummaryError(f"model did not return the expected JSON: {content[:300]!r}") \
            from exc


def _chat(messages: list[dict], model: str, url: str, timeout: float) -> dict:
    """Summary call: {"overview": str, "key_points": [str, ...]}."""
    data = _ask(messages, model, url, timeout, _SCHEMA)
    try:
        overview = str(data["overview"]).strip()
        points = [str(p).strip() for p in data["key_points"] if str(p).strip()]
    except (KeyError, TypeError) as exc:
        raise SummaryError(f"model did not return the expected JSON: {str(data)[:300]!r}") \
            from exc
    return {"overview": overview, "key_points": points}


def _keywords(messages: list[dict], model: str, url: str, timeout: float) -> list[str]:
    data = _ask(messages, model, url, timeout, _KEYWORD_SCHEMA)
    try:
        words = data["keywords"]
        if not isinstance(words, list):
            raise TypeError
    except (KeyError, TypeError) as exc:
        raise SummaryError(f"model did not return keywords: {str(data)[:300]!r}") from exc
    return [str(w) for w in words]


def clean_keywords(words: list[str], limit: int = MAX_KEYWORDS) -> list[str]:
    """Trim, drop empties and duplicates (ignoring case), keep the first `limit`."""
    seen, out = set(), []
    for w in words:
        w = " ".join(str(w).split()).strip(" .,;:#\"'")
        if w and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out[:limit]


def extract_keywords(transcript: str, model: str = DEFAULT_MODEL, url: str = DEFAULT_URL,
                     hint: str | None = None, timeout: float = 900,
                     limit: int = MAX_KEYWORDS) -> list[str]:
    """Search keywords for a transcript, most important first (at most `limit`)."""
    if not transcript.strip():
        raise SummaryError("transcript is empty")
    system = _KEYWORD_SYSTEM + (f"\nContext from the user (use it for correct names/terms): "
                                f"{hint}" if hint else "")
    parts = _chunks(transcript, CHUNK_WORDS)
    if len(parts) == 1:
        return clean_keywords(_keywords(
            [{"role": "system", "content": system},
             {"role": "user", "content": "Transcript:\n\n" + parts[0]}],
            model, url, timeout), limit)

    candidates: list[str] = []
    for i, part in enumerate(parts, start=1):
        candidates += _keywords(
            [{"role": "system", "content": system},
             {"role": "user", "content": f"Transcript, part {i} of {len(parts)}:\n\n{part}"}],
            model, url, timeout)
    candidates = clean_keywords(candidates, limit=200)
    if len(candidates) <= limit:
        return candidates
    chosen = _keywords(
        [{"role": "system", "content": system},
         {"role": "user", "content": f"These keywords were picked from consecutive parts of "
                                     f"one meeting. Choose the {limit} that best describe the "
                                     f"whole meeting for search, most important first:\n\n"
                                     + "\n".join(f"- {c}" for c in candidates)}],
        model, url, timeout)
    return clean_keywords(chosen, limit)


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

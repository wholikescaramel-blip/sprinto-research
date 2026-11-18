"""Thin Gemini wrapper. Model names come from env (GEMINI_MODEL, GEMINI_FALLBACK_MODELS); never hardcoded.

Resilience: a model that is overloaded (503) is retried briefly, then the next model in the chain is tried.
A model that is out of quota (429) is skipped immediately. Errors are classified so callers can say WHY.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel

from . import config

T = TypeVar("T", bound=BaseModel)

# Tried after GEMINI_MODEL if it is overloaded or out of quota. Model names come from the key's own model list
# (client.models.list(), 2026-11-18); override with GEMINI_FALLBACK_MODELS.
DEFAULT_FALLBACKS = "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.8-flash"
COOLDOWN_SECONDS = 90
MAX_MINUTE_WAIT = 60               # wait out a per-minute rate limit only if Google says it ends within this
MAX_DAILY_COOLDOWN = 6 * 3600      # skip a model whose DAILY quota is gone, but re-check after at most 6 hours
_COOLDOWN: dict[str, float] = {}   # model -> time before which we skip it (process-wide)

_RETRY_RE = re.compile(r"retry in ((?:\d+h)?(?:\d+m)?[\d.]+s)", re.I)


def _retry_seconds(e: Exception) -> float | None:
    """'Please retry in 16h20m59.59s' / '23.4s' -> seconds (None if Google gave no hint)."""
    m = _RETRY_RE.search(str(e))
    if not m:
        return None
    h, mi, s = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", m.group(1)).groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s)


def _is_daily(e: Exception) -> bool:
    s = str(e).lower()
    return "per day" in s or "perday" in s or "requests per day" in s


class LLMClient(Protocol):
    def generate_json(self, prompt: str, schema: type[T], system: str | None = None) -> T: ...


class LLMError(RuntimeError):
    pass


class QuotaError(LLMError):
    """Every model in the chain was out of quota (429 RESOURCE_EXHAUSTED)."""


def _is_quota(e: Exception) -> bool:
    s = str(e)
    return "RESOURCE_EXHAUSTED" in s or "429" in s


def _is_overload(e: Exception) -> bool:
    s = str(e)
    return any(t in s.lower() for t in ("503", "unavailable", "500 ", "internal", "deadline", "overloaded",
                                         "timed out", "timeout", "disconnected"))


class GeminiLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.env("GEMINI_API_KEY")
        self.model = model or config.env("GEMINI_MODEL")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set (see .env.example).")
        if not self.model:
            raise LLMError("GEMINI_MODEL is not set. Check Google's docs for the current Flash model.")
        fb = config.env("GEMINI_FALLBACK_MODELS", DEFAULT_FALLBACKS) or ""
        self.models = [self.model] + [m.strip() for m in fb.split(",") if m.strip() and m.strip() != self.model]
        from google import genai  # imported lazily so tests never need the SDK configured
        from google.genai import types
        timeout_ms = int(float(config.env("GEMINI_TIMEOUT_SECONDS", "30")) * 1000)  # a stalled call must not hang a run
        self._client = genai.Client(api_key=self.api_key, http_options=types.HttpOptions(timeout=timeout_ms))

    def _call(self, fn: Callable[[str], T], what: str) -> T:
        """fn(model) -> result. Walks the model chain; returns the first success.
        - overloaded / timing out: the model rests for COOLDOWN_SECONDS and the next model is tried at once.
        - out of quota PER DAY: the model is skipped for as long as Google says (capped), nothing to wait for.
        - out of quota PER MINUTE: if every model is limited and the shortest wait is short, wait it out once.
        A rested model is not retried on every call, so one busy model never costs a long wait each time."""
        last: Exception | None = None
        for round_ in range(2):
            order = [m for m in self.models if _COOLDOWN.get(m, 0) <= time.time()] or list(self.models)
            quota_hits, waits = 0, []
            for model in order:
                for attempt in range(2):
                    try:
                        return fn(model)
                    except Exception as e:
                        last = e
                        if _is_quota(e):
                            quota_hits += 1
                            secs = _retry_seconds(e)
                            if _is_daily(e):
                                _COOLDOWN[model] = time.time() + min(secs or 3600, MAX_DAILY_COOLDOWN)
                            else:
                                _COOLDOWN[model] = time.time() + (secs or 30)
                                waits.append(secs or 30)
                            break                  # no quota on this model right now: next one
                        if _is_overload(e):
                            _COOLDOWN[model] = time.time() + COOLDOWN_SECONDS
                            if len(order) > 1:
                                break              # another model may be free: move on at once
                            time.sleep(1.5)        # the only model: one short retry
                            continue
                        if attempt == 0:
                            time.sleep(1.5)        # a one-off bad reply (e.g. malformed JSON): retry once
                            continue
                        break
            if quota_hits == len(order) and waits and min(waits) <= MAX_MINUTE_WAIT and round_ == 0:
                time.sleep(min(waits) + 1)        # every model is rate-limited for a moment: wait, then one more pass
                continue
            break
        msg = f"Gemini {what} failed on {', '.join(order)}: {str(last)[:240]}"
        raise (QuotaError if quota_hits == len(order) else LLMError)(msg)

    def generate_json(self, prompt: str, schema: type[T], system: str | None = None) -> T:
        from google.genai import types

        def run(model: str) -> T:
            cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema,
                                              temperature=0.2, system_instruction=system)
            resp = self._client.models.generate_content(model=model, contents=prompt, config=cfg)
            return schema.model_validate_json(resp.text)
        return self._call(run, "call")

    def generate_text(self, prompt: str) -> str:
        """Plain text, no tools. Used when Google Search grounding is unavailable; callers must verify the output."""
        def run(model: str) -> str:
            return self._client.models.generate_content(model=model, contents=prompt).text or ""
        return self._call(run, "text call")

    def grounded_search(self, prompt: str) -> str:
        """Gemini with Google Search grounding (real web results). Output is only a LEAD list:
        every name/domain it returns is re-verified in code before use (see discovery.py)."""
        from google.genai import types

        def run(model: str) -> str:
            cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                              temperature=0.2)
            return self._client.models.generate_content(model=model, contents=prompt, config=cfg).text or ""
        return self._call(run, "grounded search")

"""Thin Gemini wrapper. Model name comes from GEMINI_MODEL; never hardcoded."""
from __future__ import annotations

import time
from typing import Protocol, TypeVar

from pydantic import BaseModel

from . import config

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    def generate_json(self, prompt: str, schema: type[T], system: str | None = None) -> T: ...


class LLMError(RuntimeError):
    pass


class GeminiLLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.env("GEMINI_API_KEY")
        self.model = model or config.env("GEMINI_MODEL")
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set (see .env.example).")
        if not self.model:
            raise LLMError("GEMINI_MODEL is not set. Check Google's docs for the current Flash model.")
        from google import genai  # imported lazily so tests never need the SDK configured
        self._client = genai.Client(api_key=self.api_key)

    def grounded_search(self, prompt: str) -> str:
        """Gemini with Google Search grounding (real web results). Output is only a LEAD list:
        every name/domain it returns is re-verified in code before use (see discovery.py)."""
        from google.genai import types
        cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                          temperature=0.2)
        last: Exception | None = None
        for attempt in range(3):
            try:
                return self._client.models.generate_content(
                    model=self.model, contents=prompt, config=cfg).text or ""
            except Exception as e:
                last = e
                time.sleep(2 ** attempt)
        raise LLMError(f"Gemini grounded search failed after retries: {last}")

    def generate_json(self, prompt: str, schema: type[T], system: str | None = None) -> T:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.2,
            system_instruction=system,
        )
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.models.generate_content(model=self.model, contents=prompt, config=cfg)
                return schema.model_validate_json(resp.text)
            except Exception as e:  # network, quota, malformed JSON
                last = e
                time.sleep(2 ** attempt)
        raise LLMError(f"Gemini call failed after retries: {last}")

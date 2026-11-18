"""Shared test helpers. Nothing here touches the network or paid APIs."""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from outreach.db import DB
from outreach.scraper import Fetcher

SITE = Path(__file__).parent / "fixtures" / "site"
EMPTY_RSS = "<rss version='2.0'><channel><title>x</title></channel></rss>"


class FakeLLM:
    """handlers: {SchemaClass: fn(prompt) -> schema instance or dict}"""

    def __init__(self, handlers):
        self.handlers, self.calls = handlers, []

    def generate_json(self, prompt, schema, system=None):
        self.calls.append(schema.__name__)
        out = self.handlers[schema](prompt)
        return out if isinstance(out, schema) else schema.model_validate(out)


class FakeGmail:
    """Records which Gmail API methods get called."""

    def __init__(self):
        self.calls = []

    def users(self):
        return self

    def messages(self):
        outer = self

        class M:
            def send(_, userId, body):
                outer.calls.append(("messages.send", body))
                return type("R", (), {"execute": lambda s: {"id": "msg123"}})()
        return M()

    def drafts(self):
        outer = self

        class D:
            def create(_, userId, body):
                outer.calls.append(("drafts.create", body))
                return type("R", (), {"execute": lambda s: {"id": "draft456"}})()
        return D()


def site_client(pages: dict[str, str]) -> httpx.Client:
    """Serve fixture HTML by path; everything else (incl. robots.txt) is 404."""
    def handler(req: httpx.Request) -> httpx.Response:
        name = pages.get(req.url.path)
        if name is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, text=(SITE / name).read_text(encoding="utf-8"),
                              headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


ACME_PAGES = {"/": "home.html", "/pricing": "pricing.html", "/security": "security.html",
              "/careers": "careers.html", "/about": "about.html"}


def make_fetcher(db: DB, pages=None, max_fetches=8) -> Fetcher:
    return Fetcher(db, max_fetches=max_fetches, delay=0, client=site_client(ACME_PAGES if pages is None else pages),
                   sleep=lambda s: None)


def line_with(prompt: str, phrase: str) -> tuple[str, str]:
    """(page_url, exact line) for the first page line containing phrase, parsed from an LLM prompt."""
    for m in re.finditer(r"=== PAGE: (\S+) ===\n(.*?)(?=\n=== PAGE: |\Z)", prompt, re.S):
        for line in m.group(2).split("\n"):
            if phrase in line:
                return m.group(1), line.strip()
    raise AssertionError(f"{phrase!r} not in prompt")


@pytest.fixture
def db():
    return DB(":memory:")

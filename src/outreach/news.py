"""News: negative-news veto + gap fill. Uses Google News RSS (never Google HTML) + configured RSS."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote

import feedparser
import httpx

from .db import DB
from .scraper import UA

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
NEG_TERMS = ["data breach", "breach", "hacked", "ransomware", "layoffs", "laid off", "lays off",
             "lawsuit", "sued", "sues", "shutting down", "shuts down", "shutdown", "bankruptcy",
             "insolvency", "fined"]
_NEG_RX = re.compile(r"\b(" + "|".join(map(re.escape, NEG_TERMS)) + r")\b", re.I)


def default_fetch(url: str) -> str:
    r = httpx.get(url, headers={"User-Agent": UA}, timeout=15, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _entry_date(e) -> datetime | None:
    t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _name_in(title: str, name) -> bool:
    """`name` is a company name or a list of aliases; any whole-word match counts."""
    names = [name] if isinstance(name, str) else list(name or [])
    return any(n and re.search(r"\b" + re.escape(n) + r"\b", title, re.I) is not None for n in names)


def _primary(name) -> str:
    return name if isinstance(name, str) else (name[0] if name else "")


def negative_events(feed_text: str, name: str, lookback_days: int, now: datetime | None = None):
    """Pure function: entries that are dated, recent, name the company AND contain a negative term."""
    now = now or datetime.now(timezone.utc)
    out = []
    for e in feedparser.parse(feed_text).entries:
        d = _entry_date(e)
        title = getattr(e, "title", "") or ""
        if d is None or now - d > timedelta(days=lookback_days) or d > now + timedelta(days=1):
            continue
        m = _NEG_RX.search(title)
        if m and _name_in(title, name):
            out.append({"term": m.group(1).lower(), "date": d.date().isoformat(),
                        "title": title, "link": getattr(e, "link", "") or ""})
    return out


def check_negative_news(db: DB, domain: str, name: str, lookback_days: int = 365,
                        extra_feeds: list[str] | None = None,
                        fetch: Callable[[str], str] = default_fetch, sleep=time.sleep) -> list[str]:
    """1 Google News query + 1 check per configured feed. Stores `negative_news` facts."""
    ids = []
    q = f'"{_primary(name)}" (breach OR layoffs OR lawsuit OR shutdown) when:1y'
    urls = [GOOGLE_NEWS.format(q=quote(q))] + list(extra_feeds or [])
    for url in urls:
        try:
            text = fetch(url)
        except Exception:
            continue  # fail soft: no news = pass
        for ev in negative_events(text, name, lookback_days):
            ids.append(db.add_fact(domain, "negative_news", f"{ev['term']} ({ev['date']})",
                                   ev["link"] or url, ev["title"]))
        sleep(1)
    return ids


def gap_fill_funding(db: DB, domain: str, name: str,
                     fetch: Callable[[str], str] = default_fetch) -> str | None:
    """One attempt to fill a missing funding stage from news headlines. Else stays unknown."""
    q = f'"{_primary(name)}" raises Series funding'
    try:
        text = fetch(GOOGLE_NEWS.format(q=quote(q)))
    except Exception:
        return None
    for e in feedparser.parse(text).entries:
        title = getattr(e, "title", "") or ""
        m = re.search(r"Series\s+[A-E]\b", title)
        if m and _name_in(title, name) and re.search(r"rais|funding|led", title, re.I):
            return db.add_fact(domain, "funding_stage", m.group(0), getattr(e, "link", "") or "", title)
    return None

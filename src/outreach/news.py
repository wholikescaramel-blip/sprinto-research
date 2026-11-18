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
# Precision matters: a false veto silently throws away a good prospect.
#  - "X sues Y" is NOT bad news about X (X is the plaintiff), so "sues" is not a term; "sued" is.
#  - bare "breach" also means "breach of contract"; only security breaches count.
NEG_TERMS = ["data breach", "security breach", "breached", "hacked", "ransomware", "cyberattack",
             "layoffs", "laid off", "lays off", "lawsuit", "sued", "class action",
             "shutting down", "shuts down", "shutdown", "bankruptcy", "insolvency", "fined"]
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
                        fetch: Callable[[str], str] = default_fetch, sleep=time.sleep,
                        judge: Callable[[str, list[dict]], list[int] | None] | None = None) -> list[str]:
    """1 Google News query + 1 check per configured feed.

    Headlines that match the name + a negative keyword are only CANDIDATES: names like Clay or Harvey match
    counties, fire departments and celebrities. `judge(domain, events)` returns the indices of headlines that
    are clearly about THIS company and report a real problem (or None if it cannot decide). Only judged
    headlines become `negative_news` facts (which veto). Without a verdict they are stored as
    `news_unverified` for the human to see, and never veto: a false veto silently loses a good prospect."""
    q = (f'"{_primary(name)}" (breach OR layoffs OR lawsuit OR shutdown) '
         f'("{domain}" OR software OR platform OR startup OR company) when:1y')
    urls = [GOOGLE_NEWS.format(q=quote(q))] + list(extra_feeds or [])
    candidates: list[dict] = []
    for url in urls:
        try:
            text = fetch(url)
        except Exception:
            continue  # fail soft: no news = pass
        for ev in negative_events(text, name, lookback_days):
            if not any(c["title"] == ev["title"] for c in candidates):
                candidates.append({**ev, "feed": url})
        sleep(1)
    if not candidates:
        return []
    verdict = judge(domain, candidates) if judge else None
    confirmed, field = ([candidates[i] for i in verdict if 0 <= i < len(candidates)], "negative_news") \
        if verdict is not None else (candidates, "news_unverified")
    return [db.add_fact(domain, field, f"{ev['term']} ({ev['date']})", ev["link"] or ev["feed"], ev["title"])
            for ev in confirmed]


def make_judge(llm):
    """Gemini only SELECTS from the real headlines we give it; it cannot add or reword one."""
    from .models import NewsJudgeOut

    def judge(domain: str, events: list[dict]) -> list[int] | None:
        listing = "\n".join(f"{i}. {e['title']}" for i, e in enumerate(events[:25]))
        prompt = (f"The company is the software company whose website is {domain}. These news headlines matched "
                  "its name plus a negative keyword. Return the numbers of the headlines that are clearly about "
                  "THIS company (not a person, place, county, city, sports team or other organization that merely "
                  "shares the name) AND report a security breach, layoffs at the company, a lawsuit against the "
                  "company, or the company shutting down. Exclude positive or neutral headlines (for example "
                  "'lawsuit-free deal') and anything you are unsure about.\n\n" + listing)
        try:
            out = llm.generate_json(prompt, NewsJudgeOut, "You classify headlines. Use only the headlines given.")
            return [i for i in out.about_company if 0 <= i < min(len(events), 25)]
        except Exception:
            return None  # cannot decide -> the caller treats the headlines as unverified (no veto)
    return judge


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

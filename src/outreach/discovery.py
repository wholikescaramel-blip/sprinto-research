"""Discovery: ICP-driven (RSS feeds + directory pages from config) and/or manual domain list.

A discovered domain is kept only if it literally appears in the source text or in a link on that
page. Gemini never gets to guess a domain from a company name.
"""
from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Callable

import feedparser

from .db import DB
from .llm import LLMClient
from .models import DiscoveredCompany, DiscoveryOut
from .scraper import Fetcher
from .util import registrable_domain

NOT_COMPANIES = {"linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com",
                 "google.com", "github.com", "medium.com", "crunchbase.com", "techcrunch.com",
                 "wikipedia.org", "apple.com", "reddit.com", "substack.com", "news.google.com"}
CONSUMER = {"netflix.com", "spotify.com", "tiktok.com", "amazon.com", "ebay.com"}
PARKED = re.compile(r"domain (?:name )?(?:is )?for sale|buy this domain|this domain may be for sale|"
                    r"parked (?:free|domain)|sedo\.com", re.I)

SYSTEM = ("Extract company names and their website domains from the text. A domain must appear "
          "literally in the text or in the provided links. If a company has no domain in the text, "
          "omit it. Never guess a domain from a name.")


def read_domains_file(path: str | Path) -> list[str]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        d = registrable_domain(line) if line else None
        if d:
            out.append(d)
    return out


def dedupe(domains: list[str]) -> list[str]:
    seen, out = set(), []
    for d in domains:
        r = registrable_domain(d)
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def verify_discovered(cands: list[DiscoveredCompany], text: str, links: list[str],
                      source_domain: str | None = None) -> list[DiscoveredCompany]:
    """Keep only candidates whose domain is sourced from the page. Drop everything else."""
    low = text.casefold()
    link_domains = {registrable_domain(l) for l in links}
    kept = []
    for c in cands:
        reg = registrable_domain(c.domain)
        if not reg or reg in NOT_COMPANIES or reg == source_domain:
            continue
        literal = c.domain.strip().casefold() in low or reg in low
        if literal or reg in link_domains:
            kept.append(DiscoveredCompany(company_name=c.company_name, domain=reg))
    return kept


def cheap_filter(domain: str, resolver: Callable[[str], str] = socket.gethostbyname) -> str | None:
    """Reason to drop before any deep scraping, else None."""
    if domain in CONSUMER or domain in NOT_COMPANIES:
        return "consumer/non-company site"
    try:
        resolver(domain)
    except OSError:
        return "domain does not resolve"
    return None


def is_parked(text: str) -> bool:
    return bool(PARKED.search(text)) and len(text) < 2500


def discover(db: DB, llm: LLMClient, fetcher: Fetcher, icp_cfg: dict,
             fetch_feed: Callable[[str], str] | None = None) -> list[dict]:
    """Returns [{domain, name, source_url}] and stores them as companies (origin=discovered)."""
    from .news import default_fetch
    fetch_feed = fetch_feed or default_fetch
    disc = icp_cfg.get("discovery") or {}
    found: list[dict] = []

    def process(source_url: str, text: str, links: list[str], source_domain: str | None):
        if not text.strip():
            return
        prompt = (f"Source: {source_url}\n\nLinks on the page:\n" + "\n".join(links[:200])
                  + f"\n\nText:\n{text[:12000]}")
        out = llm.generate_json(prompt, DiscoveryOut, SYSTEM)
        for c in verify_discovered(out.companies, text + "\n" + "\n".join(links), links, source_domain):
            found.append({"domain": c.domain, "name": c.company_name, "source_url": source_url})

    for url in disc.get("rss_feeds") or []:
        try:
            feed = feedparser.parse(fetch_feed(url))
        except Exception:
            continue
        text, links = [], []
        for e in feed.entries[:50]:
            text.append(f"{getattr(e, 'title', '')}\n{re.sub('<[^>]+>', ' ', getattr(e, 'summary', ''))}")
            links.append(getattr(e, "link", ""))
            links += re.findall(r'href="([^"]+)"', getattr(e, "summary", "") or "")
        process(url, "\n\n".join(text), [l for l in links if l], registrable_domain(url))
    for url in disc.get("directory_pages") or []:
        host = registrable_domain(url)
        page = fetcher.fetch(host or url, url)
        if page and page.text:
            process(url, page.text, page.links, host)

    seen = set()
    result = []
    for f in found:
        if f["domain"] in seen:
            continue
        seen.add(f["domain"])
        db.upsert_company(f["domain"], f["name"], f["source_url"], origin="discovered")
        result.append(f)
    return result


# ---------------------------------------------------------------------------------------
# ICP-driven search (no domains supplied). Gemini + Google Search grounding only PROPOSES leads.
# A lead is kept only if our own robots-aware fetch of its homepage shows the company's name.
# ---------------------------------------------------------------------------------------
LEGAL = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|gmbh|pvt|plc|co)\b\.?", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", LEGAL.sub(" ", s or "").casefold())


def default_queries(icp_cfg: dict) -> list[str]:
    custom = (icp_cfg.get("discovery") or {}).get("search_queries")
    if custom:
        return list(custom)
    icp = icp_cfg.get("icp") or {}
    ctype = icp.get("company_type", "B2B SaaS")
    stages = ", ".join(icp.get("stages", []))
    emp = icp.get("employees") or {}
    return [f"{ctype} companies that recently raised a {stages} round",
            f"{ctype} startups hiring a GRC, compliance or security lead",
            f"{ctype} startups preparing for SOC 2 or ISO 27001",
            f"{ctype} companies selling to enterprise customers that now need SOC 2",
            f"fast-growing {ctype} companies ({stages}) with {emp.get('min', 20)}-{emp.get('max', 500)} employees"]


def parse_leads(text: str) -> list[tuple[str, str]]:
    """'Name | domain.com' lines -> [(name, domain)]. Lines without a valid domain are dropped."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip(" *-\t`") for p in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            dom = registrable_domain(parts[1])
            if dom:
                out.append((parts[0], dom))
    return out


def verify_lead(db: DB, fetcher: Fetcher, name: str, domain: str,
                resolver: Callable[[str], str] = socket.gethostbyname) -> tuple[bool, str, str | None]:
    """(ok, reason, homepage_url). Proof = the company's own homepage text contains its name."""
    reason = cheap_filter(domain, resolver)
    if reason:
        return False, reason, None
    page = fetcher.fetch(domain, f"https://{domain}/") or fetcher.fetch(domain, f"https://www.{domain}/")
    if not page or page.status >= 400 or not page.text:
        return False, "homepage unreachable or blocked", None
    if is_parked(page.text):
        return False, "parked domain", None
    n = _norm(name)
    if len(n) < 3 or n not in _norm(page.text):
        return False, f"homepage does not mention '{name}'", None
    return True, "name found on homepage", page.url


def icp_search(db: DB, llm, fetcher: Fetcher, icp_cfg: dict, target: int = 20,
               max_queries: int = 8, resolver: Callable[[str], str] = socket.gethostbyname) -> list[dict]:
    """Find up to `target` NEW verified companies. Unverifiable leads are dropped, never guessed."""
    known = {r["domain"] for r in db.q("SELECT domain FROM companies")}
    found: list[dict] = []
    for q in default_queries(icp_cfg)[:max_queries]:
        if len(found) >= target:
            break
        prompt = (f"Use web search. Find up to {target} REAL companies matching: {q}.\n"
                  "Only list companies you actually found on pages in your search results. "
                  "Output one per line, exactly: Company Name | companydomain.com\n"
                  "Leave out any company whose website domain you did not see. No other text.")
        try:
            text = llm.grounded_search(prompt)
        except Exception:
            continue  # fail soft
        for name, dom in parse_leads(text):
            if dom in known or any(f["domain"] == dom for f in found):
                continue
            ok, why, home = verify_lead(db, fetcher, name, dom, resolver)
            if ok:
                db.upsert_company(dom, name, home, origin="icp_search")
                found.append({"domain": dom, "name": name, "source_url": home, "query": q})
                if len(found) >= target:
                    break
    return found


def resolve_names(db: DB, llm, fetcher: Fetcher, names: list[str],
                  resolver: Callable[[str], str] = socket.gethostbyname) -> list[dict]:
    """Company NAMES (no domain) -> verified domains, or dropped."""
    out = []
    for name in names:
        try:
            text = llm.grounded_search(f"Use web search. What is the official website domain of the "
                                       f"B2B software company '{name}'? Reply exactly: {name} | domain.com "
                                       "or UNKNOWN.")
        except Exception:
            continue
        for _n, dom in parse_leads(text.replace("UNKNOWN", ""))[:1]:
            ok, why, home = verify_lead(db, fetcher, name, dom, resolver)
            if ok:
                db.upsert_company(dom, name, home, origin="name_lookup")
                out.append({"domain": dom, "name": name, "source_url": home})
    return out


def read_inputs(path: str | Path) -> tuple[list[str], list[str]]:
    """File lines that are valid domains/URLs -> domains; everything else -> company names."""
    doms, names = [], []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        d = registrable_domain(line) if ("." in line and " " not in line) else None
        (doms if d else names).append(d or line)
    return doms, names

"""Our own scraper: robots-aware, rate-limited, cached, capped. No LinkedIn. No Google HTML."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup

from .db import DB
from .util import registrable_domain

UA = "SprintoOutreachResearchBot/0.1 (sales research; honors robots.txt)"
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com",
             "recruitee.com", "bamboohr.com", "breezy.hr", "jobvite.com", "teamtailor.com")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINKEDIN = "linkedin.com"

# category -> (guessed paths, link keyword regex)
CATEGORIES: dict[str, tuple[list[str], str]] = {
    "security": (["/security"], r"/security"),
    "pricing": (["/pricing"], r"/pricing|/plans"),
    "careers": (["/careers", "/jobs"], r"/careers?|/jobs"),
    "about": (["/about", "/company"], r"/about|/company"),
    "trust": (["/trust"], r"/trust"),
    "customers": (["/customers"], r"/customers|/case-stud"),
    "contact": (["/contact"], r"/contact"),
    "product": (["/product", "/platform"], r"/product|/platform"),
    "privacy": (["/privacy", "/privacy-policy"], r"/privacy"),
}
# fetch priority after the homepage (the cap decides how far we get)
ORDER = ["security", "pricing", "careers", "ats", "about", "trust", "customers",
         "contact", "product", "privacy"]


@dataclass
class Page:
    url: str
    status: int
    text: str
    links: list[str]
    emails: list[str]
    page_id: int | None = None
    cached: bool = False


@dataclass
class ScrapeReport:
    fetched: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cap_hit: bool = False
    time_hit: bool = False      # the wall-clock budget ran out before every wanted page was fetched
    home_ok: bool = False
    elapsed: float = 0.0        # seconds spent scraping this company (network + politeness waits)
    skipped: list[str] = field(default_factory=list)  # categories not fetched because time ran out


def parse_html(html: str, base_url: str) -> tuple[str, list[str], list[str]]:
    """Clean text (+footer, where badges live), absolute links, emails."""
    soup = BeautifulSoup(html, "html.parser")
    links, emails = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            emails.add(href[7:].split("?")[0].strip().lower())
        elif not href.startswith(("#", "javascript:", "tel:")):
            links.append(urljoin(base_url, href))
    text = trafilatura.extract(html, favor_recall=True, include_comments=False) or ""
    footer = soup.find("footer")
    if footer:
        ft = footer.get_text("\n", strip=True)
        if ft and ft not in text:
            text = f"{text}\n{ft}"
    if not text.strip():
        for t in soup(["script", "style", "noscript"]):
            t.decompose()
        text = soup.get_text("\n", strip=True)
    for m in EMAIL_RE.findall(text):
        emails.add(m.lower())
    return text.strip(), list(dict.fromkeys(links)), sorted(emails)


class Fetcher:
    def __init__(self, db: DB, max_fetches: int = 8, delay: float = 1.0,
                 client: httpx.Client | None = None, sleep=time.sleep, timeout: float = 8.0,
                 time_budget: float | None = None, clock=time.monotonic):
        """time_budget: wall-clock seconds allowed per company (None = unlimited). Never loosens robots.txt,
        the per-host delay or the fetch cap: it only adds a deadline."""
        self.db, self.max_fetches, self.delay, self.sleep = db, max_fetches, delay, sleep
        self.timeout, self.time_budget, self.clock = timeout, time_budget, clock
        self.client = client or httpx.Client(headers={"User-Agent": UA}, timeout=timeout,
                                             follow_redirects=True)
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}
        self.used: dict[str, int] = {}
        self._deadline: dict[str, float] = {}
        self.time_hit: dict[str, bool] = {}

    # -- time budget
    def start_clock(self, domain: str) -> float:
        now = self.clock()
        self.time_hit[domain] = False
        if self.time_budget:
            self._deadline[domain] = now + self.time_budget
        return now

    def remaining(self, domain: str) -> float | None:
        d = self._deadline.get(domain)
        return None if d is None else d - self.clock()

    def out_of_time(self, domain: str, need: float = 0.0) -> bool:
        """True when fewer than `need` seconds remain (a fetch needs the politeness wait plus some network)."""
        r = self.remaining(domain)
        if r is not None and r <= need:
            self.time_hit[domain] = True
            return True
        return False

    def _timeout_for(self, domain: str | None) -> float:
        r = self.remaining(domain) if domain else None
        return self.timeout if r is None else max(1.0, min(self.timeout, r))

    # -- politeness
    def _wait(self, host: str):
        gap = self.clock() - self._last.get(host, 0)
        if gap < self.delay and host in self._last:
            self.sleep(self.delay - gap)
        self._last[host] = self.clock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        if LINKEDIN in p.netloc:
            return False
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                self._wait(p.netloc)
                r = self.client.get(f"{host}/robots.txt", timeout=min(5.0, self.timeout))
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                    self._robots[host] = rp
                else:
                    self._robots[host] = None  # no robots.txt -> allowed
            except httpx.HTTPError:
                self._robots[host] = None
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(UA, url)

    def _get(self, url: str, domain: str | None = None) -> httpx.Response | None:
        host = urlparse(url).netloc
        for attempt in range(2):  # one retry only; a slow page must not eat the whole budget
            try:
                self._wait(host)
                r = self.client.get(url, timeout=self._timeout_for(domain))
                if r.status_code >= 500 and attempt < 1 and not (domain and self.out_of_time(domain, 3.0)):
                    self.sleep(1)
                    continue
                return r
            except httpx.HTTPError:
                if attempt < 1 and not (domain and self.out_of_time(domain, 3.0)):
                    self.sleep(1)
        return None

    # -- public
    def fetch(self, domain: str, url: str) -> Page | None:
        """Cached pages are free. Returns None when blocked, capped or unreachable."""
        row = self.db.get_page(url)
        if row:
            return Page(url, row["status_code"], row["text"], _j(row["links"]), _j(row["raw_emails"]),
                        row["id"], True)
        if self.used.get(domain, 0) >= self.max_fetches:
            return None
        if self.out_of_time(domain, need=self.delay + 1.0):  # cannot finish another fetch in time
            return None
        if not self.allowed(url):
            return None
        self.used[domain] = self.used.get(domain, 0) + 1
        r = self._get(url, domain)
        if r is None:
            return None
        ctype = r.headers.get("content-type", "")
        if r.status_code >= 400 or ("html" not in ctype and "xml" not in ctype and ctype):
            pid = self.db.save_page(domain, url, r.status_code, "", [], [])
            return Page(url, r.status_code, "", [], [], pid)
        text, links, emails = parse_html(r.text[:2_000_000], str(r.url))
        pid = self.db.save_page(domain, url, r.status_code, text, links, emails)
        return Page(url, r.status_code, text, links, emails, pid)

    def at_cap(self, domain: str) -> bool:
        return self.used.get(domain, 0) >= self.max_fetches


def _j(s):
    import json
    return json.loads(s) if s else []


def _same_site(link: str, domain: str) -> bool:
    return registrable_domain(link) == domain


def find_link(links: list[str], domain: str, category: str) -> str | None:
    """Prefer a link the company's own page points at over a guessed path."""
    if category == "ats":
        for l in links:
            h = urlparse(l).netloc.lower()
            if any(h == a or h.endswith("." + a) for a in ATS_HOSTS):
                return l
        return None
    rx = re.compile(CATEGORIES[category][1], re.I)
    for l in links:
        if _same_site(l, domain) and rx.search(urlparse(l).path) and urlparse(l).path.count("/") <= 3:
            return l.split("#")[0]
    if category == "trust":
        for l in links:  # trust-center subdomain linked from the site
            h = urlparse(l).netloc.lower()
            if h.startswith(("trust.", "security.")) and _same_site(l, domain):
                return l
    return None


def scrape_company(db: DB, domain: str, fetcher: Fetcher, answered=None) -> ScrapeReport:
    """Fetch homepage then categories in priority order, skipping ones whose evidence-map
    question is already answered (callable `answered(category)->bool`, uses facts so far)."""
    rep = ScrapeReport()
    t0 = fetcher.start_clock(domain)  # the per-company time budget starts here
    try:
        home = fetcher.fetch(domain, f"https://{domain}/") or fetcher.fetch(domain, f"https://www.{domain}/")
        if not home or home.status >= 400 or not home.text:
            rep.failed.append(f"https://{domain}/")
            return rep
        rep.home_ok = True
        rep.fetched.append(home.url)
        links = list(home.links)
        for i, cat in enumerate(ORDER):
            if answered and answered(cat):
                continue
            if fetcher.at_cap(domain):
                rep.cap_hit = True
                break
            # most valuable pages come first, so when time runs out only the least valuable are lost
            if fetcher.out_of_time(domain, need=fetcher.delay + 1.0):
                rep.time_hit = True
                rep.skipped = [c for c in ORDER[i:] if not (answered and answered(c))]
                break
            target = find_link(links, domain, cat)
            guesses = [] if cat == "ats" else CATEGORIES[cat][0]
            for url in ([target] if target else [f"https://{domain}{g}" for g in guesses]):
                page = fetcher.fetch(domain, url)
                if page and page.status < 400 and page.text:
                    rep.fetched.append(page.url)
                    links += page.links
                    break
                rep.failed.append(url)
                if fetcher.at_cap(domain) or fetcher.out_of_time(domain, need=fetcher.delay + 1.0):
                    break
        rep.time_hit = rep.time_hit or fetcher.time_hit.get(domain, False)
        return rep
    finally:
        rep.elapsed = round(fetcher.clock() - t0, 2)

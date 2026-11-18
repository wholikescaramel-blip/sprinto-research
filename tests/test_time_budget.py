"""30-second scrape budget: a deadline, not a downgrade. Fake clock simulates slow sites instantly."""
from __future__ import annotations

import httpx

from conftest import ACME_PAGES, EMPTY_RSS, SITE
from outreach import config
from outreach.pipeline import Services, research
from outreach.scraper import Fetcher, scrape_company


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def slow_site(clock: Clock, per_request: float, pages=None) -> httpx.Client:
    """Serves the Acmeflow fixtures; every request (including robots.txt) costs `per_request` seconds."""
    site = ACME_PAGES if pages is None else pages

    def handler(req: httpx.Request) -> httpx.Response:
        clock.t += per_request
        name = site.get(req.url.path)
        if name is None:
            return httpx.Response(404, text="nf", headers={"content-type": "text/html"})
        return httpx.Response(200, text=(SITE / name).read_text(encoding="utf-8"),
                              headers={"content-type": "text/html"})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def fetcher(db, clock, per_request, budget=30, pages=None, **kw):
    return Fetcher(db, max_fetches=8, delay=1.0, time_budget=budget, clock=clock, sleep=clock.sleep,
                   client=slow_site(clock, per_request, pages), **kw)


def test_slow_site_is_cut_off_at_the_budget(db):
    clock = Clock()
    rep = scrape_company(db, "acmeflow.io", fetcher(db, clock, per_request=10))
    assert rep.home_ok and rep.time_hit
    assert len(rep.fetched) < 8
    assert rep.elapsed <= 30 + 10  # at most one in-flight request past the line (real timeouts clamp even that)


def test_most_valuable_pages_are_fetched_first_so_only_the_least_valuable_are_lost(db):
    clock = Clock()
    rep = scrape_company(db, "acmeflow.io", fetcher(db, clock, per_request=6))
    got = " ".join(rep.fetched)
    assert rep.fetched[0].rstrip("/").endswith("acmeflow.io")  # homepage first
    assert "/security" in got and "/pricing" in got and "/careers" in got  # the evidence-heavy pages
    assert "privacy" in rep.skipped and "product" in rep.skipped          # lowest-value pages dropped


def test_without_a_budget_nothing_is_cut(db):
    clock = Clock()
    rep = scrape_company(db, "acmeflow.io", fetcher(db, clock, per_request=10, budget=None))
    assert not rep.time_hit and len(rep.fetched) == 5  # home, security, pricing, careers, about


def test_per_request_timeout_shrinks_to_what_is_left_of_the_budget(db):
    clock = Clock()
    f = fetcher(db, clock, per_request=0)
    f.start_clock("x.io")
    assert f._timeout_for("x.io") == 8.0
    clock.t = 27
    assert f._timeout_for("x.io") == 3.0
    clock.t = 29.5
    assert f._timeout_for("x.io") == 1.0  # never zero


def test_fast_sites_are_unaffected(db):
    clock = Clock()
    rep = scrape_company(db, "acmeflow.io", fetcher(db, clock, per_request=0.3))
    assert not rep.time_hit and len(rep.fetched) == 5 and rep.elapsed < 30


def test_rerun_resumes_from_cache_so_quality_is_recoverable(db):
    c1 = Clock()
    first = scrape_company(db, "acmeflow.io", fetcher(db, c1, per_request=10))
    c2 = Clock()
    second = scrape_company(db, "acmeflow.io", fetcher(db, c2, per_request=10))
    assert len(second.fetched) > len(first.fetched)  # cached pages cost no time, so new ones fit


def test_time_limit_with_unverified_b2b_is_reported_not_guessed(db):
    clock = Clock()
    # a homepage with no demo/sales/software cue: nothing on it proves B2B SaaS, and time runs out before more pages
    no_b2b_home = {"/": "about.html", "/security": "security.html", "/pricing": "pricing.html"}
    svc = Services(db=db, llm=None, fetcher=fetcher(db, clock, per_request=10, pages=no_b2b_home),
                   news_fetch=lambda u: EMPTY_RSS,
                   resolver=lambda d: "1.2.3.4", cfg=config.icp())
    r = research(svc, "acmeflow.io")
    assert r["time_hit"] and r["status"] == "insufficient_data"
    assert db.stage("acmeflow.io", "scrape") == "insufficient_data"

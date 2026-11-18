"""Mentor's order: Google News + RSS first, then our own scraper. A veto means nothing is scraped."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from conftest import EMPTY_RSS, ACME_PAGES, make_fetcher, site_client
from outreach import config
from outreach.pipeline import Services, qualify_company, research
from outreach.scraper import Fetcher


def _rss(title, when):
    return ("<rss version='2.0'><channel><title>n</title><item><title>%s</title>"
            "<link>https://news.example/a1</link><pubDate>%s</pubDate></item></channel></rss>"
            % (title, when.strftime("%a, %d %b %Y %H:%M:%S GMT")))


def _svc(db, news_fetch, fetcher):
    return Services(db=db, llm=None, fetcher=fetcher, news_fetch=news_fetch,
                    news_judge=lambda d, events: list(range(len(events))),  # judge confirms the headlines
                    resolver=lambda d: "1.2.3.4", cfg=config.icp())


def test_news_runs_before_the_first_page_is_fetched(db):
    events = []
    inner = site_client(ACME_PAGES)

    def counting(req: httpx.Request) -> httpx.Response:
        events.append("page")
        return inner.send(req) if False else inner._transport.handle_request(req)

    fetcher = Fetcher(db, max_fetches=8, delay=0, sleep=lambda s: None,
                      client=httpx.Client(transport=httpx.MockTransport(counting), follow_redirects=True))
    news = lambda url: (events.append("news"), EMPTY_RSS)[1]
    research(_svc(db, news, fetcher), "acmeflow.io")
    assert events[0] == "news" and "page" in events
    assert events.index("news") < events.index("page")


def test_confirmed_veto_skips_scraping_entirely(db):
    events = []
    recent = datetime.now(timezone.utc) - timedelta(days=20)
    news = lambda url: (events.append("news"), _rss("Acmeflow confirms data breach", recent))[1]

    def must_not_be_called(req):
        events.append("page")
        return httpx.Response(200, text="<html></html>")

    fetcher = Fetcher(db, max_fetches=8, delay=0, sleep=lambda s: None,
                      client=httpx.Client(transport=httpx.MockTransport(must_not_be_called)))
    svc = _svc(db, news, fetcher)
    r = research(svc, "acmeflow.io")
    assert r["status"] == "vetoed" and "page" not in events
    assert db.pages_for("acmeflow.io") == []
    q = qualify_company(svc, "acmeflow.io")
    assert q.decision == "NO_GO" and "negative news" in q.reasons[0].text.lower()
    assert db.stage("acmeflow.io", "scrape") == "skipped" and db.stage("acmeflow.io", "news") == "vetoed"


def test_funding_is_looked_up_up_front_from_news(db):
    recent = datetime.now(timezone.utc) - timedelta(days=60)
    news = lambda url: _rss("Acmeflow raises $30M Series B led by Fjord", recent)
    research(_svc(db, news, make_fetcher(db)), "acmeflow.io")
    stage = db.facts("acmeflow.io", "funding_stage")
    assert stage and stage[0]["value"] == "Series B" and stage[0]["source_url"].startswith("https://news.example")


def test_hyphenated_domain_matches_spaced_name_in_headlines(db):
    recent = datetime.now(timezone.utc) - timedelta(days=10)
    news = lambda url: _rss("Northwind Labs hit by ransomware attack", recent)
    r = research(_svc(db, news, make_fetcher(db, {})), "northwind-labs.io")
    assert r["status"] == "vetoed"

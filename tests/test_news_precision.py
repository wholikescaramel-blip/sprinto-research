"""A false veto silently throws away a good prospect. These came from the first real run
(Clay, ElevenLabs, Harvey all vetoed on headlines that were not bad news about them)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import EMPTY_RSS, make_fetcher
from outreach import config, news
from outreach.pipeline import Services, research


def _rss(title):
    when = datetime.now(timezone.utc) - timedelta(days=10)
    return ("<rss version='2.0'><channel><title>n</title><item><title>%s</title><link>https://n.example/1</link>"
            "<pubDate>%s</pubDate></item></channel></rss>" % (title, when.strftime("%a, %d %b %Y %H:%M:%S GMT")))


@pytest.mark.parametrize("title", [
    "Acme sues rival over patent claims",               # Acme is the plaintiff, not the subject of bad news
    "Court rules on breach of contract between Acme and a supplier",   # not a security breach
])
def test_headlines_that_are_not_bad_news_about_the_company_do_not_veto(title):
    assert news.negative_events(_rss(title), "Acme", 365) == []


@pytest.mark.parametrize("title,term", [
    ("Acme confirms security breach affecting customers", "security breach"),
    ("Acme hit by ransomware attack", "ransomware"),
    ("Acme sued by regulators", "sued"),
    ("Acme lays off 20% of staff", "lays off"),
    ("Acme files for bankruptcy", "bankruptcy"),
])
def test_real_bad_news_still_vetoes(title, term):
    (ev,) = news.negative_events(_rss(title), "Acme", 365)
    assert ev["term"] == term


CLAY = ["Former Clay County deputy files federal lawsuit alleging retaliation - Nebraska News",
        "Lowndes County plaintiffs to return to court in Project Red Clay lawsuit - WSFA"]


def _feed(*titles):
    when = datetime.now(timezone.utc) - timedelta(days=10)
    items = "".join("<item><title>%s</title><link>https://n.example/%d</link><pubDate>%s</pubDate></item>"
                    % (t, i, when.strftime("%a, %d %b %Y %H:%M:%S GMT")) for i, t in enumerate(titles))
    return f"<rss version='2.0'><channel><title>n</title>{items}</channel></rss>"


def test_homonym_headlines_judged_not_about_the_company_never_veto(db):
    db.upsert_company("clay.com")
    ids = news.check_negative_news(db, "clay.com", "Clay", 365, fetch=lambda u: _feed(*CLAY),
                                   sleep=lambda s: None, judge=lambda d, evs: [])   # the judge says: counties, not Clay
    assert ids == [] and db.facts("clay.com", "negative_news") == [] and db.facts("clay.com", "news_unverified") == []


def test_without_a_verdict_headlines_are_shown_as_unverified_and_never_veto(db):
    db.upsert_company("clay.com")
    news.check_negative_news(db, "clay.com", "Clay", 365, fetch=lambda u: _feed(*CLAY), sleep=lambda s: None,
                             judge=lambda d, evs: None)                              # model unavailable
    assert db.facts("clay.com", "negative_news") == []                              # no veto
    assert len(db.facts("clay.com", "news_unverified")) == 2                        # but the human can see them


def test_judge_only_selects_from_real_headlines_and_ignores_out_of_range_picks(db):
    from conftest import FakeLLM
    from outreach.models import NewsJudgeOut
    llm = FakeLLM({NewsJudgeOut: lambda prompt: NewsJudgeOut(about_company=[1, 99, -3])})
    events = [{"title": "a"}, {"title": "b"}]
    assert news.make_judge(llm)("x.io", events) == [1]
    broken = FakeLLM({NewsJudgeOut: lambda prompt: (_ for _ in ()).throw(RuntimeError("503"))})
    assert news.make_judge(broken)("x.io", events) is None                         # unavailable -> no verdict


def test_the_judge_prompt_names_the_company_and_excludes_good_news(db):
    from conftest import FakeLLM
    from outreach.models import NewsJudgeOut
    seen = []
    llm = FakeLLM({NewsJudgeOut: lambda prompt: (seen.append(prompt), NewsJudgeOut())[1]})
    news.make_judge(llm)("clay.com", [{"title": "Clay County lawsuit"}])
    assert "clay.com" in seen[0] and "lawsuit-free" in seen[0] and "Clay County lawsuit" in seen[0]


def test_a_stale_veto_from_an_old_run_does_not_outlive_the_rule_that_made_it(db):
    db.upsert_company("acmeflow.io")
    db.add_fact("acmeflow.io", "negative_news", "sues (2026-11-02)", "https://n.example/old", "Acmeflow sues X")
    svc = Services(db=db, llm=None, fetcher=make_fetcher(db), news_fetch=lambda u: EMPTY_RSS,
                   resolver=lambda d: "1.2.3.4", cfg=config.icp())
    r = research(svc, "acmeflow.io")
    assert r["status"] != "vetoed" and db.facts("acmeflow.io", "negative_news") == []

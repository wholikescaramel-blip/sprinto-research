"""Run policy: find N GOOD-FIT companies and stop; never research zero-fit leads; draft GO and GO_FLAGGED only."""
from __future__ import annotations

import pytest

from conftest import FakeLLM, make_fetcher
from outreach import config, pipeline
from outreach.discovery import icp_search
from outreach.models import QualResult
from outreach.pipeline import Services, run_pipeline, research


def Q(domain, decision="GO", score=6.0):
    return QualResult(domain=domain, decision=decision, score=score,
                      passes_threshold=decision in ("GO", "GO_FLAGGED") and score >= 3)


@pytest.fixture
def rig(db, monkeypatch):
    """Stub the expensive stages so only the orchestration policy is under test."""
    log = {"researched": [], "pocs": [], "drafts": [], "queries": []}
    verdicts: dict[str, QualResult] = {}
    leads: list[str] = []

    monkeypatch.setattr(pipeline, "research", lambda svc, d, l=None: (log["researched"].append(d) or {"domain": d}))
    monkeypatch.setattr(pipeline, "qualify_company", lambda svc, d: verdicts[d])
    monkeypatch.setattr(pipeline, "poc_company", lambda svc, d: (log["pocs"].append(d) or type("P", (), {"status": "ok"})()))
    monkeypatch.setattr(pipeline, "draft_company", lambda svc, d: (log["drafts"].append(d) or len(log["drafts"])))
    monkeypatch.setattr(pipeline, "discover", lambda *a, **k: [])

    def fake_search(db_, llm, fetcher, cfg, target=8, only_query=None, **k):
        log["queries"].append(only_query)
        return [{"domain": d, "name": d} for d in leads] if only_query == 0 else []
    monkeypatch.setattr("outreach.discovery.icp_search", fake_search)
    svc = Services(db=db, llm=object(), resolver=lambda d: "1.2.3.4", cfg=config.icp())
    return svc, verdicts, leads, log


def test_discovery_stops_the_moment_it_has_the_target_number_of_good_fits(rig):
    svc, verdicts, leads, log = rig
    leads += ["a.io", "b.io", "c.io", "d.io", "e.io"]
    verdicts.update({"a.io": Q("a.io"), "b.io": Q("b.io", "NO_GO", 0), "c.io": Q("c.io", "GO_FLAGGED", 4),
                     "d.io": Q("d.io"), "e.io": Q("e.io")})
    s = run_pipeline(svc, [], min_companies=3)
    assert log["researched"] == ["a.io", "b.io", "c.io", "d.io"]       # e.io never researched: 3 good fits reached
    assert [d for d, *_ in s["qualified"]] == ["a.io", "d.io", "c.io"] or len(s["qualified"]) == 3
    assert log["drafts"] == ["a.io", "c.io", "d.io"] or sorted(log["drafts"]) == ["a.io", "c.io", "d.io"]
    assert any("found 3 good-fit companies after researching 4" in n for n in s["notes"])


def test_no_go_and_below_threshold_companies_are_never_drafted(rig):
    svc, verdicts, leads, log = rig
    leads += ["x.io", "y.io", "z.io"]
    verdicts.update({"x.io": Q("x.io", "NO_GO", 0), "y.io": Q("y.io", "INSUFFICIENT_DATA", 0),
                     "z.io": Q("z.io", "GO_FLAGGED", 2)})          # flagged but below the send threshold
    run_pipeline(svc, [], min_companies=3)
    assert log["pocs"] == [] and log["drafts"] == []


def test_discovery_never_researches_more_than_four_times_the_target(rig):
    svc, verdicts, leads, log = rig
    leads += [f"n{i}.io" for i in range(30)]
    verdicts.update({d: Q(d, "NO_GO", 0) for d in leads})
    s = run_pipeline(svc, [], min_companies=3)
    assert len(log["researched"]) == 12 and any("found 0 of the 3" in n for n in s["notes"])


def test_explicit_domains_are_researched_in_full_even_if_they_turn_out_zero_fit(rig):
    svc, verdicts, leads, log = rig
    verdicts.update({"mine.io": Q("mine.io", "NO_GO", 0), "yours.io": Q("yours.io", "NO_GO", 0)})
    run_pipeline(svc, ["mine.io", "yours.io"], min_companies=1)
    assert log["researched"] == ["mine.io", "yours.io"] and log["queries"] == []   # no discovery was run


def test_a_draft_failure_is_reported_and_does_not_lose_the_company(rig, monkeypatch):
    svc, verdicts, leads, log = rig
    leads.append("a.io")
    verdicts["a.io"] = Q("a.io")
    monkeypatch.setattr(pipeline, "draft_company", lambda s, d: (_ for _ in ()).throw(RuntimeError("429 quota")))
    s = run_pipeline(svc, [], min_companies=1)
    assert [d for d, *_ in s["qualified"]] == ["a.io"] and "a.io" in s["errors"]
    assert any("Draft email" in n for n in s["notes"])             # tells the user how to retry


def test_a_lead_with_no_b2b_software_signal_on_its_homepage_is_never_researched(db):
    class Leads:
        def grounded_search(self, p):
            return "Acmeflow | acmeflow.io"
    # the homepage here is the About page: it names the company but has no demo/sales/software cue
    plain = make_fetcher(db, {"/": "about.html"})
    notes: list[str] = []
    assert icp_search(db, Leads(), plain, config.icp(), 5, 1, lambda d: "1.2.3.4", notes=notes, b2b_gate=True) == []
    assert any("no B2B software signal" in n for n in notes) and db.one("SELECT COUNT(*) n FROM companies")["n"] == 0
    from outreach.db import DB
    fresh = DB(":memory:")                                          # (the first call cached the plain page in `db`)
    ok = icp_search(fresh, Leads(), make_fetcher(fresh), config.icp(), 5, 1, lambda d: "1.2.3.4", b2b_gate=True)
    assert [f["domain"] for f in ok] == ["acmeflow.io"]            # a real B2B homepage passes the gate


def test_scraping_stops_early_when_the_pages_already_prove_a_hard_no_go(db):
    db.upsert_company("acmeflow.io")
    for field, val in (("cert_soc2_type2", "SOC 2 Type II"), ("cert_iso27001", "ISO 27001"), ("competitor_tool", "Vanta")):
        db.add_fact("acmeflow.io", field, val, "https://acmeflow.io/security", val)
    called = []
    llm = FakeLLM({})
    llm.generate_json = lambda *a, **k: called.append(1)             # must never be called for a proven NO_GO
    svc = Services(db=db, llm=llm, fetcher=make_fetcher(db), news_fetch=lambda u: "<rss><channel/></rss>",
                   news_judge=lambda d, e: [], resolver=lambda d: "1.2.3.4", cfg=config.icp())
    research(svc, "acmeflow.io")
    assert called == [] and [p["url"] for p in db.pages_for("acmeflow.io")] == ["https://acmeflow.io/"]
    assert pipeline.qualify_company(svc, "acmeflow.io").decision == "NO_GO"

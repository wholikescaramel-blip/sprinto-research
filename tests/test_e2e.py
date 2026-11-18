"""Whole pipeline on recorded fixtures with fake Gemini/Gmail: scrape -> ... -> TEST email -> explain."""
from __future__ import annotations

import base64
import email
import json

from conftest import EMPTY_RSS, FakeGmail, FakeLLM, line_with, make_fetcher
from outreach import config
from outreach.explain import explain
from outreach.models import (DraftOut, DraftSentence, ExtractionOut, FieldOut, PainPick, PainPicksOut,
                             PersonOut)
from outreach.pipeline import Services, run_pipeline
from outreach.review import record_review
from outreach.send import deliver
from outreach.feedback import aggregate, record_outcome


def extraction(prompt):
    u, l = line_with(prompt, "Book a demo")
    pu, pl = line_with(prompt, "Priya Nair")
    return ExtractionOut(
        company_name=FieldOut(value="Acmeflow", source_url=u,
                              snippet="Acmeflow is a cloud platform that helps operations teams "
                                      "automate approvals across their business."),
        is_b2b_saas=FieldOut(value="true", source_url=u, snippet=l),
        people=[PersonOut(name="Priya Nair", title="Head of Security", source_url=pu, snippet=pl)])


def make_llm(job_id_holder):
    def pains(prompt):
        return PainPicksOut(pain_points=[
            PainPick(pain_id="sec_evidence", rationale="SOC 2 work in progress", fact_ids=[]),
            PainPick(pain_id="sec_questionnaires", rationale="enterprise tier", fact_ids=[])])

    def draft(prompt):
        facts = json.loads(prompt.split("Verified company facts: ")[1].split("\nSprinto facts")[0])
        job = next(f["id"] for f in facts if f["field"] == "grc_job")
        job_holder = job_id_holder
        job_holder["id"] = job
        return DraftOut(subject="Quick question on GRC hiring", greeting_name="Priya", sentences=[
            DraftSentence(text="Saw you're hiring a Head of GRC.", type="hook", fact_ids=[job]),
            DraftSentence(text="Is collecting audit evidence across tools eating your team's week?",
                          type="pain", fact_ids=[job]),
            DraftSentence(text="Sprinto automates compliance certifications such as SOC 2, ISO 27001 "
                               "and GDPR for SaaS companies.", type="pitch", fact_ids=["S1"]),
            DraftSentence(text="Open to a quick chat?", type="cta")])
    return FakeLLM({ExtractionOut: extraction, PainPicksOut: pains, DraftOut: draft})


def test_full_pipeline_to_test_email(db, monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "me@example.com")
    monkeypatch.setattr(config, "manual_pocs", lambda: {
        "acmeflow.io": {"name": "Priya Nair", "title": "Head of Security", "email": "priya@acmeflow.io",
                        "source": "apollo_manual"}})
    holder = {}
    llm = make_llm(holder)
    svc = Services(db=db, llm=llm, fetcher=make_fetcher(db), news_fetch=lambda u: EMPTY_RSS,
                   resolver=lambda d: "1.2.3.4", cfg=config.icp())

    summary = run_pipeline(svc, ["acmeflow.io", "https://www.acmeflow.io/"])  # deduped
    assert summary["companies"] == 1 and not summary["errors"], summary
    assert llm.calls.count("ExtractionOut") == 1  # hard cap: one extraction call

    q = db.get_qual("acmeflow.io")
    # trigger (3) + headcount 85 in range (2); cert status never stated -> flagged, not dropped
    assert q.decision == "GO_FLAGGED" and q.passes_threshold and q.score == 5
    assert q.unknowns == ["cert_status"]
    fields = {r["field"] for r in db.facts("acmeflow.io")}
    assert {"grc_job", "soc2_in_progress", "enterprise_signal", "is_b2b_saas", "headcount"} <= fields

    poc = db.get_poc("acmeflow.io")
    assert poc.name == "Priya Nair" and poc.source == "apollo_manual" and poc.email_status == "verified"

    (d,) = db.drafts_for("acmeflow.io")
    assert d["status"] == "pending_review", d["check_result"]

    record_review(db, d["id"], "approve", "good", "other", "looks right")
    gm = FakeGmail()
    r = deliver(db, d["id"], service=gm, mode="TEST")
    assert r["status"] == "sent_test"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(gm.calls[0][1]["raw"]))
    body = msg.get_payload(decode=True).decode()
    assert msg["To"] == "me@example.com" and "Intended recipient: Priya Nair <priya@acmeflow.io>" in body
    assert "Hi Priya," in body and "Head of GRC" in body

    text = explain(db, "acmeflow.io")
    assert "Why this company" in text and "Why this person" in text and "Why the email" in text
    assert "https://acmeflow.io/careers" in text  # sources are cited

    record_outcome(db, d["id"], "replied")
    agg = aggregate(db)
    assert agg["labels"] == {"good": 1} and agg["outcomes"] == {"replied": 1}


def test_unreachable_domain_fails_soft(db):
    svc = Services(db=db, llm=None, fetcher=make_fetcher(db, {}), news_fetch=lambda u: EMPTY_RSS,
                   resolver=lambda d: "1.2.3.4", cfg=config.icp())
    s = run_pipeline(svc, ["ghost.io", "acmeflow.io"])
    assert "ghost.io" in s["errors"] and s["companies"] == 2  # run continued

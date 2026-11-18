"""Draft for every GO / GO_FLAGGED company (even with no contact yet), Unflag, and the preloaded sample results."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeLLM
from outreach import api, config, samples
from outreach.db import DB
from outreach.models import (DraftOut, DraftSentence, PainPick, PainPicksOut, Poc, QualResult)


def draft_handler(prompt):
    facts = json.loads(prompt.split("Verified company facts: ")[1].split("\nSprinto facts")[0])
    job = next(f["id"] for f in facts if f["field"] == "grc_job")
    greeting = "there" if "greeting_name MUST be exactly 'there'" in prompt else "Priya"
    return DraftOut(subject="Your GRC opening", greeting_name=greeting, sentences=[
        DraftSentence(text="Saw you're hiring a Head of GRC.", type="hook", fact_ids=[job]),
        DraftSentence(text="Is collecting audit evidence across tools eating the team's week?", type="pain", fact_ids=[job]),
        DraftSentence(text="Sprinto covers SOC 2, ISO 27001, HIPAA, GDPR, PCI DSS and 200+ global standards.",
                      type="pitch", fact_ids=["S1"]),
        DraftSentence(text="Open to a quick chat?", type="cta")])


def make_llm():
    return FakeLLM({PainPicksOut: lambda p: PainPicksOut(pain_points=[PainPick(pain_id="ceo_deals", rationale="x"),
                                                                       PainPick(pain_id="ceo_cost", rationale="y")]),
                    DraftOut: draft_handler})


def seed(db, decision="GO_FLAGGED", score=5.0, poc=None, domain="x.io"):
    db.upsert_company(domain, "X", f"https://{domain}/", origin="icp_search")
    db.add_fact(domain, "company_name", "X", f"https://{domain}/", "X is a platform for teams.")
    db.add_fact(domain, "grc_job", "Head of GRC", f"https://{domain}/careers", "Head of GRC")
    db.save_qual(QualResult(domain=domain, decision=decision, score=score, confidence="medium",
                            unknowns=["stage_fit"] if decision == "GO_FLAGGED" else [],
                            passes_threshold=decision in ("GO", "GO_FLAGGED") and score >= 3))
    if poc:
        db.save_poc(poc)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(api, "llm_factory", make_llm)
    monkeypatch.setattr(samples, "SAMPLE_FILE", tmp_path / "seed" / "sample_results.json")
    return TestClient(api.app)


def test_a_good_fit_with_no_contact_still_gets_a_hi_there_draft_that_cannot_be_sent(client):
    seed(api.get_db(), poc=Poc(domain="x.io", status="no_named_poc", reason="Hunter had nobody"))
    r = client.post("/api/company/x.io/draft").json()
    assert r["ok"] and r["draft_status"] == "pending_review"
    (item,) = client.get("/api/review-queue").json()
    assert item["body"].startswith("Hi there,") and item["check"]["passed"]
    assert any("needs_contact" in w for w in item["check"]["warnings"])
    client.post(f"/api/drafts/{item['id']}/review", json={"action": "approve", "label": "good", "reason_tag": "other"})
    sent = client.post(f"/api/drafts/{item['id']}/send")
    assert sent.status_code == 400 and "no named PoC" in sent.json()["detail"]      # never emailed without a contact


def test_a_named_contact_gets_a_named_greeting(client):
    seed(api.get_db(), poc=Poc(domain="x.io", status="ok", name="Priya Nair", title="CEO", email="p@x.io",
                               email_status="sourced", role_type="founder_ceo"))
    client.post("/api/company/x.io/draft")
    (item,) = client.get("/api/review-queue").json()
    assert item["body"].startswith("Hi Priya,")


def test_no_go_and_below_threshold_companies_cannot_be_drafted(client):
    db = api.get_db()
    seed(db, "NO_GO", 0, domain="no.io")
    seed(db, "GO_FLAGGED", 2, domain="low.io")
    assert client.post("/api/company/no.io/draft").status_code == 400
    assert client.post("/api/company/low.io/draft").status_code == 400
    assert {c["domain"]: c["can_draft"] for c in client.get("/api/companies").json()} == {"no.io": False, "low.io": False}


def test_unflag_marks_go_and_regenerates_the_draft_superseding_the_old_one(client):
    db = api.get_db()
    seed(db, poc=Poc(domain="x.io", status="ok", name="Priya Nair", title="CEO", email="p@x.io",
                     email_status="sourced", role_type="founder_ceo"))
    client.post("/api/company/x.io/draft")
    old = client.get("/api/review-queue").json()[0]["id"]
    assert client.get("/api/companies").json()[0]["can_unflag"] is True
    r = client.post("/api/company/x.io/unflag").json()
    assert r["ok"] and r["decision"] == "GO" and r["draft_status"] == "pending_review" and r["draft_id"] != old
    q = db.get_qual("x.io")
    assert q.decision == "GO" and q.unknowns == [] and q.confidence == "human-verified" and q.passes_threshold
    assert "Unflagged by a reviewer" in q.reasons[-1].text
    assert db.get_draft(old)["status"] == "superseded"                              # replaced, not deleted
    assert [d["id"] for d in client.get("/api/review-queue").json()] == [r["draft_id"]]
    assert client.get("/api/companies").json()[0]["can_unflag"] is False


def test_only_flagged_companies_can_be_unflagged(client):
    seed(api.get_db(), "GO", 6)
    assert client.post("/api/company/x.io/unflag").status_code == 400
    assert client.post("/api/company/nope.io/unflag").status_code == 404


def test_unflag_still_succeeds_when_the_redraft_fails_and_says_how_to_retry(client, monkeypatch):
    seed(api.get_db(), poc=Poc(domain="x.io", status="ok", name="Priya Nair", title="CEO", email="p@x.io",
                               email_status="sourced", role_type="founder_ceo"))
    monkeypatch.setattr(api, "llm_factory", lambda: (_ for _ in ()).throw(RuntimeError("429 quota")))
    r = client.post("/api/company/x.io/unflag").json()
    assert r["ok"] and r["decision"] == "GO" and r["draft_id"] is None and "Draft email" in r["draft_error"]
    assert api.get_db().get_qual("x.io").decision == "GO"                            # the human's decision was kept
    assert client.get("/api/companies").json()[0]["can_draft"] is True              # and the retry button is offered


# ---- preloaded sample results ------------------------------------------------------------------------
def test_samples_export_mask_emails_then_load_and_clear(client, tmp_path):
    src = DB(":memory:")
    seed(src, poc=Poc(domain="x.io", status="ok", name="Priya Nair", title="CEO", email="priya@x.io",
                      email_status="verified", role_type="founder_ceo"))
    svc_llm = make_llm()
    from outreach.draft import make_draft
    make_draft(src, "x.io", svc_llm)
    out = samples.export_samples(src, ["x.io", "missing.io"], note="real run")
    text = json.dumps(out)
    assert "priya@x.io" not in text and "p***@x.io" in text and len(out["companies"]) == 1   # masked, missing skipped

    assert client.get("/api/status").json()["samples_available"] is True
    assert client.post("/api/samples/load").json()["loaded"] == 1
    c = client.get("/api/companies").json()[0]
    assert c["origin"] == "sample" and c["decision"] == "GO_FLAGGED" and c["draft_status"] == "pending_review"
    assert client.get("/api/status").json()["samples_loaded"] == 1
    assert client.get("/api/company/x.io").json()["facts"]                          # facts with sources came along
    assert client.post("/api/samples/clear").json()["cleared"] == 1
    assert client.get("/api/companies").json() == []


def test_loading_samples_never_overwrites_a_real_result(client):
    db = api.get_db()
    src = DB(":memory:")
    seed(src)
    samples.export_samples(src, ["x.io"])
    seed(db, "GO", 9)                                                               # a REAL result for the same domain
    db.x("UPDATE companies SET origin='manual' WHERE domain='x.io'")
    assert client.post("/api/samples/load").json()["loaded"] == 0
    assert db.get_qual("x.io").score == 9 and client.post("/api/samples/clear").json()["cleared"] == 0


def test_samples_endpoint_404s_when_a_deployment_has_no_sample_file(client):
    assert client.get("/api/status").json()["samples_available"] is False
    assert client.post("/api/samples/load").status_code == 404

"""The hosted (Vercel) flow: the browser calls one step per company. No network, no real Gemini/Hunter/Resend."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import make_fetcher
from outreach import api, config, pipeline
from outreach.pipeline import Services
from test_e2e import make_llm


PG_TABLES = ("facts, pages, qualifications, pocs, hunter_cache, counters, drafts, reviews, outcomes, "
             "rubric_versions, runs, stage_status, companies")


def _wipe_pg(url: str):
    # ONLY for a throwaway/empty database named in TEST_DATABASE_URL. Never point it at real data.
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        try:
            c.execute(f"TRUNCATE {PG_TABLES} RESTART IDENTITY CASCADE")
        except psycopg.errors.UndefinedTable:
            pass  # first run: tables do not exist yet


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("PUBLIC_MODE", "1")
    pg = os.environ.get("TEST_DATABASE_URL")
    if pg:  # run the same flow on real Postgres (set TEST_DATABASE_URL to an empty scratch database)
        monkeypatch.setenv("DATABASE_URL", pg)
        _wipe_pg(pg)
    else:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "hosted.db"))
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    monkeypatch.setattr(config, "manual_pocs", lambda: {
        "acmeflow.io": {"name": "Priya Nair", "title": "Head of Security", "email": "priya@acmeflow.io",
                        "source": "apollo_manual"}})
    # no network: fixture site, empty news, DNS always resolves
    monkeypatch.setattr(Services, "get_fetcher", lambda self: make_fetcher(self.db))
    monkeypatch.setattr(pipeline, "check_negative_news", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "gap_fill_funding", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "cheap_filter", lambda *a, **k: None)
    monkeypatch.setattr(api, "llm_factory", lambda: make_llm({}))
    yield TestClient(api.app)
    if pg:
        _wipe_pg(pg)


def test_status_says_hosted_and_forces_test_mode(hosted, monkeypatch):
    monkeypatch.setenv("SEND_MODE", "LIVE")  # even if misconfigured, the public app reports TEST
    s = hosted.get("/api/status").json()
    assert s["hosted"] is True and s["mode"] == "TEST"


def test_full_step_flow_then_review_queue_masks_contact_emails(hosted):
    d = hosted.post("/api/step/discover", json={"inputs": "acmeflow.io"}).json()
    assert [c["domain"] for c in d["companies"]] == ["acmeflow.io"] and d["next_query"] is None

    r = hosted.post("/api/step/research", json={"domain": "acmeflow.io"}).json()
    assert r["ok"] and r["passes"] and r["decision"] == "GO_FLAGGED"

    dr = hosted.post("/api/step/draft", json={"domain": "acmeflow.io"}).json()
    assert dr["poc_status"] == "ok" and dr["draft_status"] == "pending_review"

    (item,) = hosted.get("/api/review-queue").json()
    assert item["poc"]["email"] == "p***@acmeflow.io"          # masked on the public app
    assert "priya@" not in hosted.get("/api/company/acmeflow.io").text
    # the real address is still stored (the local app and the send gate need it)
    assert config.manual_pocs()["acmeflow.io"]["email"] == "priya@acmeflow.io"


def test_public_app_never_starts_a_background_run_and_validates_input(hosted):
    assert hosted.post("/api/run", json={"inputs": ""}).status_code == 409
    assert hosted.post("/api/step/research", json={"domain": "not a domain"}).status_code == 400
    assert hosted.post("/api/step/draft", json={"domain": "acmeflow.io"}).status_code == 400  # not researched yet


def test_daily_cap_stops_runaway_use_without_a_password(hosted, monkeypatch):
    monkeypatch.setenv("DAILY_RESEARCH_CAP", "1")
    assert hosted.post("/api/step/research", json={"domain": "acmeflow.io"}).status_code == 200
    over = hosted.post("/api/step/research", json={"domain": "acmeflow.io"})
    assert over.status_code == 429 and "Daily limit" in over.json()["detail"]


def test_local_use_is_never_capped_or_masked(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.setenv("OUTREACH_DB", str(tmp_path / "local.db"))
    monkeypatch.setenv("DAILY_RESEARCH_CAP", "0")
    db = api.get_db()
    api._cap(db, "research", "DAILY_RESEARCH_CAP", 0)  # would raise 429 if capping applied locally
    assert api._mask("a@b.com") == "a***@b.com"        # the masker itself works...
    assert TestClient(api.app).get("/api/status").json()["hosted"] is False  # ...but only runs in public mode

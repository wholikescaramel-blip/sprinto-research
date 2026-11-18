"""UI backend: runs on a temp DB, never touches Gmail/Gemini."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from outreach import api, config
from outreach.db import DB
from outreach.models import CheckResult, Poc


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "db_path", lambda: tmp_path / "t.db")
    db = DB(tmp_path / "t.db")
    db.upsert_company("x.io", "X")
    fid = db.add_fact("x.io", "grc_job", "Head of GRC", "https://x.io/careers", "Head of GRC")
    poc = Poc(domain="x.io", status="ok", name="Priya Nair", title="Head of Security", email="p@x.io",
              email_status="sourced", source="hunter", role_type="security_head")
    db.save_poc(poc)
    db.save_draft(domain="x.io", subject="s", body="Hi Priya,\n\nSaw the GRC role.\n\nBest,\nKay Dee",
                  sentences=json.dumps([{"text": "Saw the GRC role.", "type": "hook", "fact_ids": [fid]}]),
                  pain_points="[]", greeting_name="Priya", rubric_version=1, status="pending_review",
                  check_result=CheckResult(passed=True).model_dump_json(), poc=poc.model_dump_json())
    return TestClient(api.app)


def test_status_companies_and_queue(client):
    assert client.get("/api/status").json()["drafts"] == {"pending_review": 1}
    assert client.get("/api/companies").json()[0]["domain"] == "x.io"
    q = client.get("/api/review-queue").json()
    assert len(q) == 1 and q[0]["poc"]["name"] == "Priya Nair"


def test_trace_expands_facts_with_sources(client):
    d = client.get("/api/company/x.io").json()
    f = d["drafts"][0]["sentences"][0]["facts"][0]
    assert f["source_url"] == "https://x.io/careers" and f["snippet"] == "Head of GRC"
    assert client.get("/api/company/nope.io").status_code == 404


def test_review_validates_and_moves_draft(client):
    bad = client.post("/api/drafts/1/review", json={"action": "approve", "label": "meh", "reason_tag": "other"})
    assert bad.status_code == 400
    ok = client.post("/api/drafts/1/review", json={"action": "approve", "label": "good", "reason_tag": "other"})
    assert ok.status_code == 200
    assert client.get("/api/review-queue").json()[0]["status"] == "approved"


def test_send_is_blocked_until_approved(client):
    r = client.post("/api/drafts/1/send")
    assert r.status_code == 400 and "approved" in r.json()["detail"]  # nothing reaches Gmail

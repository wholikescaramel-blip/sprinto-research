"""Preloaded SAMPLE results: real pipeline output captured to seed/sample_results.json so a reviewer can see how
every stage looks (facts with sources, Go/No-Go reasons, contact, claim-checked draft) even when live scraping or
Gemini is unavailable. Nothing here is invented: `export_samples` snapshots companies the pipeline really researched,
and contact emails are masked. Sample rows are tagged origin='sample' so they can be cleared in one click."""
from __future__ import annotations

import json
from pathlib import Path

from . import config
from .db import DB, now
from .models import Poc, QualResult
from .util import mask_emails

SAMPLE_FILE = config.ROOT / "seed" / "sample_results.json"
LIVE_DRAFTS = ("pending_review", "blocked", "approved")


def available(path: Path | None = None) -> bool:
    return (path or SAMPLE_FILE).exists()


def loaded_count(db: DB) -> int:
    return db.one("SELECT COUNT(*) n FROM companies WHERE origin='sample'")["n"]


def export_samples(db: DB, domains: list[str], path: Path | None = None, note: str = "") -> dict:
    """Snapshot the named companies (facts, qualification, contact, live drafts) into a JSON file."""
    out = {"captured_at": now(), "note": note, "companies": []}
    for d in domains:
        c = db.one("SELECT * FROM companies WHERE domain=?", (d,))
        q = db.get_qual(d)
        if not c or not q:
            continue
        poc = db.get_poc(d)
        drafts = [dict(x) for x in db.drafts_for(d) if x["status"] in LIVE_DRAFTS]
        out["companies"].append(mask_emails({
            "domain": d, "name": c["name"], "source_url": c["source_url"],
            "facts": [{k: f[k] for k in ("id", "field", "value", "source_url", "snippet", "fetched_at")}
                      for f in db.facts(d)],
            "qualification": q.model_dump(),
            "poc": poc.model_dump() if poc else None,
            "drafts": [{k: x[k] for k in ("subject", "body", "sentences", "pain_points", "greeting_name",
                                          "rubric_version", "status", "check_result", "poc")} for x in drafts],
        }))
    p = path or SAMPLE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def load_samples(db: DB, path: Path | None = None) -> int:
    """Insert the sample companies (origin='sample'). Never touches a company that already has real data."""
    data = json.loads((path or SAMPLE_FILE).read_text(encoding="utf-8"))
    n = 0
    for c in data["companies"]:
        d = c["domain"]
        existing = db.one("SELECT origin FROM companies WHERE domain=?", (d,))
        if existing and existing["origin"] != "sample":
            continue  # a real result for the same company wins
        if existing:
            clear_company(db, d)
        db.upsert_company(d, c.get("name"), c.get("source_url"), origin="sample")
        for f in c["facts"]:
            db.x("INSERT INTO facts(id,domain,field,value,source_url,snippet,page_id,fetched_at,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,NULL,?,?,?) ON CONFLICT DO NOTHING",
                 (f["id"], d, f["field"], f["value"], f["source_url"], f["snippet"], f["fetched_at"], now(), now()))
        db.save_qual(QualResult.model_validate(c["qualification"]))
        if c.get("poc"):
            db.save_poc(Poc.model_validate(c["poc"]))
        for x in c["drafts"]:
            db.save_draft(domain=d, subject=x["subject"], body=x["body"], sentences=x["sentences"],
                          pain_points=x["pain_points"], greeting_name=x["greeting_name"],
                          rubric_version=x["rubric_version"], status="pending_review" if x["status"] == "approved" else x["status"],
                          check_result=x["check_result"], poc=x["poc"])
        for stage in ("news", "scrape", "extract", "qualify", "poc", "draft"):
            db.set_stage(d, stage, "sample")
        n += 1
    return n


def clear_company(db: DB, d: str) -> None:
    ids = [r["id"] for r in db.q("SELECT id FROM drafts WHERE domain=?", (d,))]
    for i in ids:
        db.x("DELETE FROM reviews WHERE draft_id=?", (i,))
        db.x("DELETE FROM outcomes WHERE draft_id=?", (i,))
    for t in ("drafts", "facts", "qualifications", "pocs", "stage_status", "pages"):
        db.x(f"DELETE FROM {t} WHERE domain=?", (d,))
    db.x("DELETE FROM companies WHERE domain=?", (d,))


def clear_samples(db: DB) -> int:
    doms = [r["domain"] for r in db.q("SELECT domain FROM companies WHERE origin='sample'")]
    for d in doms:
        clear_company(db, d)
    return len(doms)

"""Local web API + UI host. Run:  uvicorn outreach.api:app --port 8000
Everything stays on this machine: keys, SQLite, Gmail token. Nothing here is deployed anywhere."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .db import DB, now
from .feedback import aggregate, record_outcome
from .models import CheckResult
from .pipeline import Services, run_pipeline
from .review import REASON_TAGS, record_review
from .send import SendBlocked, deliver

WEB = Path(__file__).resolve().parents[2] / "web"
app = FastAPI(title="Sprinto outreach")

_lock = threading.Lock()
RUN: dict = {"running": False, "log": [], "stage": None, "found": 0, "summary": None, "error": None,
             "started": None, "finished": None}


def get_db() -> DB:
    return DB(config.db_path())  # one connection per request/thread (sqlite is not thread-shared)


# ------------------------------------------------------------------ status
@app.get("/api/status")
def status():
    db = get_db()
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM drafts GROUP BY status")}
    q = {r["decision"]: r["n"] for r in db.q("SELECT decision, COUNT(*) n FROM qualifications GROUP BY decision")}
    return {
        "mode": config.send_mode(),
        "my_email_set": bool(config.env("MY_EMAIL")),
        "gemini_ready": bool(config.env("GEMINI_API_KEY") and config.env("GEMINI_MODEL")),
        "hunter_ready": bool(config.env("HUNTER_API_KEY")),
        "hunter_used": db.counter("hunter", period),
        "gmail_ready": (config.ROOT / "token.json").exists(),
        "demo": "demo" in config.db_path().name,
        "companies": db.one("SELECT COUNT(*) n FROM companies")["n"],
        "qualifications": q,
        "drafts": counts,
        "reason_tags": REASON_TAGS,
        "run": {k: RUN[k] for k in ("running", "stage", "found", "started", "finished")},
    }


# ------------------------------------------------------------------ companies
@app.get("/api/companies")
def companies():
    db = get_db()
    out = []
    for c in db.q("SELECT * FROM companies ORDER BY updated_at DESC"):
        d = c["domain"]
        q = db.get_qual(d)
        poc = db.get_poc(d)
        drafts = db.drafts_for(d)
        stages = {s["stage"]: s["status"] for s in db.q("SELECT stage,status FROM stage_status WHERE domain=?", (d,))}
        name = db.facts(d, "company_name")
        out.append({
            "domain": d, "name": (name[0]["value"] if name else c["name"]) or d, "origin": c["origin"],
            "decision": q.decision if q else None, "score": q.score if q else None,
            "confidence": q.confidence if q else None, "unknowns": q.unknowns if q else [],
            "passes": q.passes_threshold if q else False,
            "top_reason": next((r.text for r in q.reasons if not r.text.startswith("B2B SaaS confirmed")),
                               q.reasons[0].text if q.reasons else None) if q else None,
            "poc": ({"name": poc.name, "title": poc.title, "status": poc.status,
                     "email_status": poc.email_status} if poc else None),
            "draft_status": drafts[-1]["status"] if drafts else None,
            "draft_id": drafts[-1]["id"] if drafts else None,
            "stages": stages, "n_facts": len(db.facts(d)),
        })
    return out


def _fact(db: DB, domain: str, fid: str) -> dict:
    r = db.fact_by_id(domain, fid)
    if r:
        return {"id": fid, "field": r["field"], "value": r["value"], "snippet": r["snippet"],
                "source_url": r["source_url"], "fetched_at": r["fetched_at"]}
    sp = next((f for f in config.sprinto_facts().get("facts", []) if f["id"] == fid), None)
    return {"id": fid, "field": "sprinto_claim", "value": sp["text"] if sp else "?", "snippet": None,
            "source_url": "config/sprinto_facts.yaml", "fetched_at": None}


@app.get("/api/company/{domain}")
def company(domain: str):
    db = get_db()
    c = db.one("SELECT * FROM companies WHERE domain=?", (domain,))
    if not c:
        raise HTTPException(404, "unknown company")
    q = db.get_qual(domain)
    poc = db.get_poc(domain)
    drafts = []
    for d in db.drafts_for(domain):
        drafts.append({
            "id": d["id"], "status": d["status"], "subject": d["subject"], "body": d["body"],
            "rubric_version": d["rubric_version"], "gmail_id": d["gmail_id"], "sent_mode": d["sent_mode"],
            "check": json.loads(d["check_result"] or "{}"),
            "pain_points": [{**p, "facts": [_fact(db, domain, f) for f in p.get("fact_ids", [])]}
                            for p in json.loads(d["pain_points"] or "[]")],
            "sentences": [{**s, "facts": [_fact(db, domain, f) for f in s.get("fact_ids", [])]}
                          for s in json.loads(d["sentences"] or "[]")],
            "reviews": [dict(r) for r in db.q("SELECT action,label,reason_tag,note,diff FROM reviews WHERE draft_id=?", (d["id"],))],
        })
    return {
        "domain": domain, "origin": c["origin"], "source_url": c["source_url"],
        "stages": [dict(s) for s in db.q("SELECT stage,status,error FROM stage_status WHERE domain=?", (domain,))],
        "qualification": None if not q else {
            **q.model_dump(exclude={"reasons"}),
            "reasons": [{"text": r.text, "facts": [_fact(db, domain, f) for f in r.fact_ids]} for r in q.reasons]},
        "poc": poc.model_dump() if poc else None,
        "facts": [dict(f) for f in db.facts(domain)],
        "drafts": drafts,
    }


# ------------------------------------------------------------------ run
class RunReq(BaseModel):
    inputs: str = ""          # one domain or company name per line; empty = ICP search
    min_companies: int = 10
    limit: Optional[int] = None


def _run_job(req: RunReq):
    from .discovery import registrable_domain
    try:
        from .llm import GeminiLLM
        db = DB(config.db_path())
        svc = Services(db=db, llm=GeminiLLM(), cfg=config.icp())
        doms, names = [], []
        for line in req.inputs.splitlines():
            line = line.split("#")[0].strip()
            if not line:
                continue
            d = registrable_domain(line) if ("." in line and " " not in line) else None
            (doms if d else names).append(d or line)

        def progress(msg: str):
            with _lock:
                if msg.startswith("stage:"):
                    RUN["stage"] = msg.split("|")[0][6:]
                    RUN["log"].append(msg.split("|", 1)[1])
                elif msg.startswith("found:"):
                    RUN["found"] = int(msg.split("|")[0][6:])
                    RUN["log"].append(msg.split("|", 1)[1])
                else:
                    RUN["log"].append(msg.split("|", 1)[1] if "|" in msg else msg)

        s = run_pipeline(svc, doms, False, req.limit, names, req.min_companies, progress)
        with _lock:
            RUN["summary"] = s
    except Exception as e:  # surfaced in the UI, never crashes the server
        with _lock:
            RUN["error"] = str(e)[:500]
    finally:
        with _lock:
            RUN["running"] = False
            RUN["finished"] = now()


@app.post("/api/run")
def start_run(req: RunReq):
    with _lock:
        if RUN["running"]:
            raise HTTPException(409, "a run is already in progress")
        RUN.update(running=True, log=[], stage="discover", found=0, summary=None, error=None,
                   started=now(), finished=None)
    threading.Thread(target=_run_job, args=(req,), daemon=True).start()
    return {"started": True}


@app.get("/api/run")
def run_state():
    with _lock:
        return dict(RUN)


# ------------------------------------------------------------------ review + send
@app.get("/api/review-queue")
def review_queue():
    db = get_db()
    out = []
    for d in db.q("SELECT * FROM drafts WHERE status IN ('pending_review','blocked','approved') ORDER BY id"):
        q = db.get_qual(d["domain"])
        poc = json.loads(d["poc"] or "{}")
        name = db.facts(d["domain"], "company_name")
        out.append({
            "id": d["id"], "domain": d["domain"], "company": name[0]["value"] if name else d["domain"],
            "status": d["status"], "subject": d["subject"], "body": d["body"],
            "check": json.loads(d["check_result"] or "{}"),
            "score": q.score if q else None, "decision": q.decision if q else None,
            "reasons": [r.text for r in q.reasons][:4] if q else [],
            "poc": poc, "sentences": json.loads(d["sentences"] or "[]"),
        })
    return out


class ReviewReq(BaseModel):
    action: str
    label: str
    reason_tag: str = "other"
    note: str = ""
    final_text: Optional[str] = None


@app.post("/api/drafts/{draft_id}/review")
def review(draft_id: int, r: ReviewReq):
    try:
        record_review(get_db(), draft_id, r.action, r.label, r.reason_tag, r.note, r.final_text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/drafts/{draft_id}/send")
def send(draft_id: int):
    try:
        return deliver(get_db(), draft_id)
    except SendBlocked as e:
        raise HTTPException(400, f"Blocked: {e}")
    except Exception as e:
        raise HTTPException(502, f"Gmail error: {str(e)[:300]}")


class OutcomeReq(BaseModel):
    outcome: str


@app.post("/api/drafts/{draft_id}/outcome")
def outcome(draft_id: int, r: OutcomeReq):
    try:
        record_outcome(get_db(), draft_id, r.outcome)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/feedback")
def feedback():
    return aggregate(get_db())


@app.get("/api/sprinto-claims")
def claims():
    return config.sprinto_facts().get("facts", [])


# ------------------------------------------------------------------ UI
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")

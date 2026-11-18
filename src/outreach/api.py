"""Local web API + UI host. Run:  uvicorn outreach.api:app --port 8000
Local by default: keys, SQLite and the Resend key stay on this machine."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .db import DB, now
from .feedback import aggregate, record_outcome
from .models import CheckResult
from .pipeline import (Services, draft_company, poc_company, qualify_company, research,
                       run_pipeline)
from .review import REASON_TAGS, record_review
from .send import SendBlocked, SendError, deliver, public_mode
from .util import registrable_domain

WEB = Path(__file__).resolve().parents[2] / "web"
app = FastAPI(title="Sprinto outreach")

_lock = threading.Lock()
RUN: dict = {"running": False, "log": [], "stage": None, "found": 0, "summary": None, "error": None,
             "started": None, "finished": None}
llm_factory = None  # tests inject a fake; production builds Gemini lazily


def get_db() -> DB:
    return DB(config.db_path())  # one connection per request (SQLite file locally, Postgres when hosted)


def make_llm():
    if llm_factory:
        return llm_factory()
    from .llm import GeminiLLM
    return GeminiLLM()


def _cap(db: DB, name: str, env_key: str, default: int) -> None:
    """Daily budget for the public app (no login, so a runaway loop must not drain the keys).
    Atomic counter in the shared DB; local use is never capped."""
    if not public_mode():
        return
    limit = int(config.env(env_key, str(default)))
    if db.bump_counter(name, datetime.now(timezone.utc).strftime("%Y-%m-%d")) > limit:
        raise HTTPException(429, f"Daily limit reached for the public app ({limit} {name} steps a day). "
                                 "Try again tomorrow, or run the app locally.")


# Contacts' addresses came from Hunter. On the public app they are shown masked (p***@acme.com).
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _mask(o):
    if isinstance(o, str):
        return _EMAIL_RE.sub(lambda m: m.group(1) + "***" + m.group(2), o)
    if isinstance(o, list):
        return [_mask(x) for x in o]
    if isinstance(o, dict):
        return {k: _mask(v) for k, v in o.items()}
    return o


@app.middleware("http")
async def mask_public_emails(request: Request, call_next):
    resp = await call_next(request)
    if not (public_mode() and request.url.path.startswith("/api/")
            and resp.headers.get("content-type", "").startswith("application/json")):
        return resp
    body = b"".join([chunk async for chunk in resp.body_iterator])
    try:
        return JSONResponse(_mask(json.loads(body)), status_code=resp.status_code)
    except ValueError:
        return Response(body, status_code=resp.status_code, media_type="application/json")


# ------------------------------------------------------------------ status
@app.get("/api/status")
def status():
    db = get_db()
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM drafts GROUP BY status")}
    q = {r["decision"]: r["n"] for r in db.q("SELECT decision, COUNT(*) n FROM qualifications GROUP BY decision")}
    return {
        "mode": "TEST" if public_mode() else config.send_mode(),
        "hosted": public_mode(),
        "my_email_set": bool(config.env("MY_EMAIL")),
        "gemini_ready": bool(config.env("GEMINI_API_KEY") and config.env("GEMINI_MODEL")),
        "hunter_ready": bool(config.env("HUNTER_API_KEY")),
        "hunter_used": db.counter("hunter", period),
        "mail_ready": bool(config.env("RESEND_API_KEY")),
        "demo": "demo" in str(config.db_path()),
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
            "rubric_version": d["rubric_version"], "message_id": d["message_id"], "sent_mode": d["sent_mode"],
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
    if public_mode():  # serverless functions cannot keep a background thread alive: the browser drives /api/step/*
        raise HTTPException(409, "The hosted app runs step by step (see /api/step/*).")
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


# ------------------------------------------------------------------ step API (hosted: one company per request)
class StepDiscoverReq(BaseModel):
    inputs: str = ""
    query_index: int = 0


class StepReq(BaseModel):
    domain: str


def _domain_or_400(raw: str) -> str:
    d = registrable_domain(raw)
    if not d:
        raise HTTPException(400, f"not a valid domain: {raw[:60]}")
    return d


@app.post("/api/step/discover")
def step_discover(req: StepDiscoverReq):
    """Manual domains/names resolve in the first call. With no input, each call runs ONE ICP search query
    (Gemini + Google Search; every lead is verified against its own homepage) and says which query is next."""
    from .discovery import default_queries, dedupe, icp_search, parse_inputs, resolve_names
    db, cfg = get_db(), config.icp()
    doms, names = parse_inputs(req.inputs)
    found: list[dict] = []
    notes: list[str] = []
    if doms or names:
        if req.query_index == 0:
            for d in dedupe(doms):
                db.upsert_company(d, origin="manual")
                found.append({"domain": d, "name": None})
            if names:
                _cap(db, "discover", "DAILY_DISCOVER_CAP", 30)
                svc = Services(db=db, llm=make_llm(), cfg=cfg)
                found += [{"domain": r["domain"], "name": r["name"]}
                          for r in resolve_names(db, svc.llm, svc.get_fetcher(), names[:5], notes=notes)]
        return {"companies": found, "next_query": None, "notes": notes}
    queries = default_queries(cfg)
    i = max(0, req.query_index)
    if i >= len(queries):
        return {"companies": [], "next_query": None}
    _cap(db, "discover", "DAILY_DISCOVER_CAP", 30)
    svc = Services(db=db, llm=make_llm(), cfg=cfg)
    got = icp_search(db, svc.llm, svc.get_fetcher(), cfg, target=8, only_query=i, max_verify=6, notes=notes)
    return {"companies": [{"domain": g["domain"], "name": g["name"]} for g in got],
            "next_query": i + 1 if i + 1 < len(queries) else None, "notes": notes}


@app.post("/api/step/research")
def step_research(req: StepReq):
    """News + RSS first, then our scraper, extraction and Go/No-Go for ONE company."""
    dom = _domain_or_400(req.domain)
    db = get_db()
    _cap(db, "research", "DAILY_RESEARCH_CAP", 40)
    svc = Services(db=db, llm=make_llm(), cfg=config.icp())
    r = research(svc, dom)
    if r.get("dropped") or r.get("status") == "failed":
        return {"domain": dom, "ok": False, "note": r.get("dropped") or "homepage unreachable or blocked"}
    q = qualify_company(svc, dom)
    return {"domain": dom, "ok": True, "decision": q.decision, "score": q.score,
            "passes": q.passes_threshold, "status": r.get("status"), "elapsed": r.get("elapsed")}


@app.post("/api/step/draft")
def step_draft(req: StepReq):
    """Find the contact (Hunter, cached) and draft + claim-check the email for ONE qualified company."""
    dom = _domain_or_400(req.domain)
    db = get_db()
    q = db.get_qual(dom)
    if q is None or not q.passes_threshold:
        raise HTTPException(400, "this company has not qualified; research it first")
    _cap(db, "draft", "DAILY_DRAFT_CAP", 25)
    svc = Services(db=db, llm=make_llm(), cfg=config.icp())
    poc = poc_company(svc, dom)
    did = draft_company(svc, dom) if poc.status != "no_named_poc" else None
    d = db.get_draft(did) if did else None
    return {"domain": dom, "poc_status": poc.status, "draft_id": did, "draft_status": d["status"] if d else None}


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
        db = get_db()
        _cap(db, "send", "DAILY_SEND_CAP", 10)  # public app only; sends go to the owner's inbox (TEST) at most
        return deliver(db, draft_id)
    except SendBlocked as e:
        raise HTTPException(400, f"Blocked: {e}")
    except SendError as e:
        raise HTTPException(502, str(e)[:300])
    except Exception as e:
        raise HTTPException(502, f"Email error: {str(e)[:300]}")


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
if WEB.exists():  # locally FastAPI serves the UI; on Vercel the CDN serves it from public/
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

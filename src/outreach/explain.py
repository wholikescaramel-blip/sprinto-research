"""`outreach explain <domain>`: why this company, why this person, why this email - with sources."""
from __future__ import annotations

import json

from .db import DB
from .models import CheckResult


def _fact_lines(db: DB, domain: str, ids: list[str], indent: str = "      ") -> list[str]:
    out = []
    for fid in ids:
        r = db.fact_by_id(domain, fid)
        if r:
            out.append(f'{indent}[{fid}] {r["field"]} = {r["value"]!r}\n{indent}  "{r["snippet"]}"\n'
                       f'{indent}  source: {r["source_url"]}  (fetched {r["fetched_at"]})')
        else:
            out.append(f"{indent}[{fid}] (not a company fact - e.g. a Sprinto fact id)")
    return out


def explain(db: DB, domain: str) -> str:
    L: list[str] = [f"# {domain}"]
    c = db.one("SELECT * FROM companies WHERE domain=?", (domain,))
    if not c:
        return f"No data for {domain}. Run `outreach research {domain}` first."
    L.append(f"origin: {c['origin']}  source: {c['source_url'] or 'manual list'}")
    for s in db.q("SELECT * FROM stage_status WHERE domain=? ORDER BY updated_at", (domain,)):
        L.append(f"stage {s['stage']}: {s['status']}" + (f" ({s['error'][:200]})" if s["error"] else ""))

    q = db.get_qual(domain)
    L.append("\n## Why this company")
    if not q:
        L.append("  (not qualified yet)")
    else:
        L.append(f"  decision={q.decision} score={q.score} confidence={q.confidence} "
                 f"passes_threshold={q.passes_threshold} rubric=v{q.rubric_version}")
        for r in q.reasons:
            L.append(f"  - {r.text}")
            L += _fact_lines(db, domain, r.fact_ids)
        if q.unknowns:
            L.append(f"  unknown (flagged, never used in the email): {', '.join(q.unknowns)}")

    L.append("\n## Why this person")
    poc = db.get_poc(domain)
    if not poc:
        L.append("  (no PoC search yet)")
    else:
        L.append(f"  {poc.name or '-'} | {poc.title or '-'} | {poc.email or '-'}")
        L.append(f"  status={poc.status} email_status={poc.email_status} source={poc.source} "
                 f"confidence={poc.confidence} verification={poc.verification}")
        L.append(f"  why: {poc.reason}")
        if poc.guessed_email:
            L.append(f"  (unverified suggestion for a human only: {poc.guessed_email})")

    L.append("\n## Why the email says what it says")
    drafts = db.drafts_for(domain)
    if not drafts:
        L.append("  (no draft)")
    for d in drafts:
        cr = CheckResult.model_validate_json(d["check_result"])
        L.append(f"  draft {d['id']} status={d['status']} rubric=v{d['rubric_version']} "
                 f"checker={'PASS' if cr.passed else 'FAIL'}")
        if cr.failures:
            L.append(f"    failures: {cr.failures}")
        if cr.warnings:
            L.append(f"    warnings: {cr.warnings}")
        L.append(f"    subject: {d['subject']}\n" + "\n".join("    | " + l for l in d["body"].splitlines()))
        for p in json.loads(d["pain_points"] or "[]"):
            L.append(f"    pain hypothesis [{p['id']}] {p['text']} - {p['rationale']}")
            L += _fact_lines(db, domain, p["fact_ids"], "        ")
        for s in json.loads(d["sentences"] or "[]"):
            L.append(f"    ({s['type']}) {s['text']}")
            L += _fact_lines(db, domain, s["fact_ids"], "        ")
        for r in db.q("SELECT * FROM reviews WHERE draft_id=?", (d["id"],)):
            L.append(f"    review: {r['action']} label={r['label']} tag={r['reason_tag']} note={r['note']!r}")
        if d["gmail_id"]:
            L.append(f"    delivered: mode={d['sent_mode']} gmail_id={d['gmail_id']}")
    return "\n".join(L)

"""Human review gate: approve / edit / discard + good|bad label + reason tag."""
from __future__ import annotations

import difflib
import json

import typer

from .db import DB
from .models import CheckResult

REASON_TAGS = ["wrong_hook", "wrong_person", "too_generic", "tone", "factually_off", "bad_fit", "other"]
ACTIONS = ("approve", "edit", "discard")
LABELS = ("good", "bad")


def make_diff(original: str, final: str) -> str:
    return "\n".join(difflib.unified_diff(original.splitlines(), final.splitlines(),
                                          "original", "final", lineterm=""))


def record_review(db: DB, draft_id: int, action: str, label: str, reason_tag: str,
                  note: str = "", final_text: str | None = None) -> int:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    if reason_tag not in REASON_TAGS:
        raise ValueError(f"reason_tag must be one of {REASON_TAGS}")
    d = db.get_draft(draft_id)
    if d is None:
        raise ValueError(f"no draft {draft_id}")
    original = d["body"]
    final = original
    if action == "edit":
        if not final_text or final_text.strip() == original.strip():
            raise ValueError("edit requires changed text")
        final = final_text
    rid = db.x("""INSERT INTO reviews(draft_id,action,label,reason_tag,note,original_text,final_text,diff,
                  created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
               (draft_id, action, label, reason_tag, note, original, final,
                make_diff(original, final) if action == "edit" else ""))
    if action == "discard":
        db.update_draft(draft_id, status="discarded")
    else:  # approve / edit: the (possibly edited) body is what gets delivered
        db.update_draft(draft_id, status="approved", body=final)
    return rid


def show_draft(db: DB, d) -> None:
    q = db.get_qual(d["domain"])
    cr = CheckResult.model_validate_json(d["check_result"])
    typer.secho(f"\n=== Draft {d['id']}  {d['domain']} ===", bold=True)
    if q:
        typer.echo(f"Score {q.score} ({q.decision}, confidence {q.confidence})")
        for r in q.reasons:
            typer.echo(f"  - {r.text}  {r.fact_ids}")
        if q.unknowns:
            typer.echo(f"  unknown (not used in email): {q.unknowns}")
    poc = json.loads(d["poc"])
    typer.echo(f"To: {poc.get('name')} <{poc.get('email')}> [{poc.get('email_status')}] - {poc.get('reason')}")
    typer.echo(f"\nSubject: {d['subject']}\n\n{d['body']}\n")
    typer.echo(f"Checker: {'PASS' if cr.passed else 'FAIL'}  warnings={cr.warnings}  failures={cr.failures}")


def interactive(db: DB) -> int:
    drafts = db.q("SELECT * FROM drafts WHERE status='pending_review' ORDER BY id")
    blocked = db.q("SELECT id,domain FROM drafts WHERE status='blocked'")
    if blocked:
        typer.echo(f"{len(blocked)} blocked draft(s) need a human (see `outreach explain <domain>`): "
                   + ", ".join(f"{b['id']}:{b['domain']}" for b in blocked))
    done = 0
    for d in drafts:
        show_draft(db, d)
        action = typer.prompt("Action [approve/edit/discard/skip]", default="skip").strip().lower()
        if action == "skip":
            continue
        if action not in ACTIONS:
            typer.echo("unknown action, skipping")
            continue
        final = None
        if action == "edit":
            final = typer.edit(d["body"])
            if final is None or final.strip() == d["body"].strip():
                typer.echo("no changes made; skipping")
                continue
        label = typer.prompt("Label [good/bad]", default="good" if action != "discard" else "bad")
        tag = typer.prompt(f"Reason tag {REASON_TAGS}", default="other")
        note = typer.prompt("Note (optional)", default="", show_default=False)
        try:
            record_review(db, d["id"], action, label, tag, note, final)
            done += 1
        except ValueError as e:
            typer.secho(str(e), fg="red")
    return done

"""Feedback log aggregation + rubric proposals. Humans approve every change; hard gates are untouchable."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config
from .db import DB, now
from .llm import LLMClient
from .models import RubricProposalOut

OUTCOMES = ("bounced", "replied", "positive_reply")


def record_outcome(db: DB, draft_id: int, outcome: str) -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    if db.get_draft(draft_id) is None:
        raise ValueError(f"no draft {draft_id}")
    db.x("INSERT INTO outcomes(draft_id,outcome,created_at,updated_at) VALUES(?,?,?,?)",
         (draft_id, outcome, now(), now()))


def aggregate(db: DB) -> dict:
    reviews = db.q("SELECT r.*, d.subject, d.rubric_version FROM reviews r JOIN drafts d ON d.id=r.draft_id")
    outcomes = db.q("SELECT outcome, COUNT(*) n FROM outcomes GROUP BY outcome")
    ex = lambda lab: [{"subject": r["subject"], "final_text": r["final_text"][:600], "tag": r["reason_tag"]}
                      for r in reviews if r["label"] == lab][:5]
    return {
        "n_reviews": len(reviews),
        "actions": dict(Counter(r["action"] for r in reviews)),
        "labels": dict(Counter(r["label"] for r in reviews)),
        "reason_tags": dict(Counter(r["reason_tag"] for r in reviews)),
        "edit_diffs": [r["diff"][:800] for r in reviews if r["action"] == "edit"][:8],
        "good_examples": ex("good"), "bad_examples": ex("bad"),
        "outcomes": {o["outcome"]: o["n"] for o in outcomes},
        "notes": [r["note"] for r in reviews if r["note"]][:10],
    }


SYSTEM = ("You improve a cold-email drafting rubric from reviewer feedback. Propose SPECIFIC, small rule "
          "changes. You may only change (a) drafting_guidance lines or (b) soft-signal weights "
          "(compliance_trigger, stage_fit, eu_regulated). Never touch hard gates, facts or Sprinto claims.")


def propose_rubric(db: DB, llm: LLMClient, out_dir: Path | None = None) -> Path:
    agg = aggregate(db)
    if agg["n_reviews"] == 0:
        raise ValueError("no reviews logged yet; review some drafts first")
    rub = config.rubric()
    prompt = (f"Current rubric (v{rub.get('version')}):\n{yaml.safe_dump(rub)}\n"
              f"Current weights: {config.weights(rub)}\n"
              f"Aggregated feedback:\n{yaml.safe_dump(agg)}\n"
              "Return changes[]: target 'drafting_guidance' (new_guidance_line) or 'soft_weights' "
              "(weight_key, new_weight), each with a description grounded in the feedback.")
    out = llm.generate_json(prompt, RubricProposalOut, SYSTEM)
    changes = []
    for c in out.changes:  # enforce the boundary in code, not just in the prompt
        if c.target == "soft_weights" and c.weight_key in config.SOFT_WEIGHT_KEYS and c.new_weight is not None:
            changes.append(c.model_dump())
        elif c.target == "drafting_guidance" and c.new_guidance_line:
            changes.append(c.model_dump())
    d = out_dir or (config.CONFIG_DIR / "rubric_proposals")
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"proposal_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.yaml"
    path.write_text(yaml.safe_dump({"based_on_version": rub.get("version"), "status": "pending",
                                    "changes": changes, "feedback_summary": agg}, sort_keys=False),
                    encoding="utf-8")
    return path


def approve_proposal(db: DB, proposal_path: Path, approved: list[int] | None = None,
                     config_dir: Path | None = None) -> Path:
    """Apply human-approved changes (indices; None = all) as rubric_v{N+1}.yaml. Never edits old versions."""
    prop = yaml.safe_load(Path(proposal_path).read_text(encoding="utf-8"))
    base = config.rubric(int(prop["based_on_version"]))
    new = {"version": int(config.latest_rubric_version()) + 1,
           "drafting_guidance": list(base.get("drafting_guidance", [])),
           "soft_weights": dict(base.get("soft_weights") or {})}
    for i, c in enumerate(prop["changes"]):
        if approved is not None and i not in approved:
            continue
        if c["target"] == "drafting_guidance":
            new["drafting_guidance"].append(c["new_guidance_line"])
        elif c["target"] == "soft_weights" and c["weight_key"] in config.SOFT_WEIGHT_KEYS:
            new["soft_weights"][c["weight_key"]] = c["new_weight"]
    path = (config_dir or config.CONFIG_DIR) / f"rubric_v{new['version']}.yaml"
    path.write_text(yaml.safe_dump(new, sort_keys=False), encoding="utf-8")
    t = now()
    db.x("INSERT INTO rubric_versions(version,path,source_proposal,created_at,updated_at) "
         "VALUES(?,?,?,?,?) ON CONFLICT(version) DO UPDATE SET path=excluded.path, "
         "source_proposal=excluded.source_proposal, updated_at=excluded.updated_at",
         (new["version"], str(path), str(proposal_path), t, t))
    return path

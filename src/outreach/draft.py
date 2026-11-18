"""Pain points + cold-email draft (Gemini sees verified facts only), then the claim checker."""
from __future__ import annotations

import json

from . import config
from .checker import check_draft
from .db import DB
from .llm import LLMClient
from .models import CheckResult, DraftOut, PainPicksOut, Poc

# facts Gemini may see. No raw pages, no other people, no competitor names, no inbox addresses.
DRAFT_FIELDS = {"company_name", "product_description", "funding_stage", "headcount", "grc_job",
                "soc2_in_progress", "iso27001_in_progress", "enterprise_signal", "named_customer",
                "eu_regulated", "cert_soc2_type2", "cert_soc2_type1", "cert_soc2", "cert_iso27001"}
HOOK_FIELDS = {"grc_job", "soc2_in_progress", "iso27001_in_progress", "enterprise_signal",
               "funding_stage", "named_customer"}

PAIN_SYSTEM = ("You pick likely pain points for a sales email. They are HYPOTHESES, not facts. "
               "Use only the fact ids provided.")
DRAFT_SYSTEM = ("You write short, human cold emails. You may only state things supported by the "
                "verified facts (cite their ids) or by the Sprinto facts (cite S-ids). Pain points are "
                "hypotheses: phrase them as questions or 'teams like yours often' observations, never as "
                "facts about the company. Do not mention anything not in the facts. No buzzwords.")


def fact_payload(rows) -> list[dict]:
    return [{"id": r["id"], "field": r["field"], "value": r["value"], "snippet": r["snippet"],
             "source_url": r["source_url"]} for r in rows if r["field"] in DRAFT_FIELDS]


def has_hook(rows) -> bool:
    return any(r["field"] in HOOK_FIELDS for r in rows)


def pick_pain_points(llm: LLMClient, poc: Poc, facts: list[dict], library: dict) -> list[dict]:
    role = poc.role_type or "founder_ceo"
    options = library.get(role) or []
    if not options:
        return []
    ids = {o["id"] for o in options}
    prompt = (f"Recipient: {poc.name}, {poc.title} (role type: {role}).\n"
              f"Pain point options (pick exactly 2 by pain_id):\n{json.dumps(options)}\n"
              f"Verified facts about their company:\n{json.dumps(facts)}\n"
              "For each pick give a one-sentence rationale and the fact_ids that make it plausible "
              "(empty list if it is a role-level default).")
    out = llm.generate_json(prompt, PainPicksOut, PAIN_SYSTEM)
    valid_facts = {f["id"] for f in facts}
    picks, text = [], {o["id"]: o["text"] for o in options}
    for p in out.pain_points:
        if p.pain_id in ids and p.pain_id not in [x["id"] for x in picks]:
            picks.append({"id": p.pain_id, "text": text[p.pain_id], "rationale": p.rationale,
                          "fact_ids": [f for f in p.fact_ids if f in valid_facts]})
    for o in options:  # top up from the library, clearly marked as a default
        if len(picks) >= 2:
            break
        if o["id"] not in [x["id"] for x in picks]:
            picks.append({"id": o["id"], "text": o["text"],
                          "rationale": "role-level default (model pick missing/invalid)", "fact_ids": []})
    return picks[:2]


def build_draft_prompt(poc: Poc, facts: list[dict], sprinto: dict, pains: list[dict], rubric: dict,
                       failures: list[str] | None = None) -> str:
    sfacts = [{"id": f["id"], "text": f["text"], "use_when": f.get("use_when", "")}
              for f in sprinto.get("facts", [])]
    p = (f"Write a cold email to {poc.name} ({poc.title}). Greeting uses first name: {poc.first_name}.\n"
         f"Return JSON: subject, greeting_name, sentences[{{text,type(hook|pain|pitch|cta|other),fact_ids}}].\n"
         "Structure: ONE hook sentence built on a verified fact (type hook, cite its id); ONE pain point "
         "phrased as a question (type pain, cite company fact ids); ONE short pitch sentence (type pitch, "
         "cite S-ids) built from the ONE Sprinto fact whose use_when best fits THIS prospect's verified "
         "facts (skip facts that do not fit; never use a fact just because it exists; use_when is guidance, "
         "not a claim); ONE soft call to action (type cta). 80-120 words total.\n"
         f"Rules (rubric v{rubric.get('version')}): {json.dumps(rubric.get('drafting_guidance', []))}\n"
         f"Verified company facts: {json.dumps(facts)}\n"
         f"Sprinto facts (the ONLY allowed claims about Sprinto): {json.dumps(sfacts)}\n"
         f"Pain points to choose from (hypotheses): {json.dumps(pains)}\n")
    if failures:
        p += "Your previous draft was rejected. Fix these problems:\n- " + "\n- ".join(failures) + "\n"
    return p


def assemble_body(d: DraftOut, signature: str) -> str:
    paras, cur = [], []
    for s in d.sentences:
        cur.append(s.text.strip())
        if s.type in ("hook", "pain", "pitch"):
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return f"Hi {d.greeting_name},\n\n" + "\n\n".join(paras) + f"\n\n{signature.strip()}"


def make_draft(db: DB, domain: str, llm: LLMClient, poc: Poc | None = None) -> int | None:
    """Returns the draft id, or None when there is nobody to address (no_named_poc)."""
    poc = poc or db.get_poc(domain)
    qual = db.get_qual(domain)
    if poc is None or qual is None:
        raise RuntimeError("run qualify and poc first")
    if poc.status == "no_named_poc" or not poc.name:
        return None
    rows = db.facts(domain)
    facts = fact_payload(rows)
    sprinto = config.sprinto_facts()
    rub = config.rubric()
    by_id = {r["id"]: r for r in rows}
    sp_text = {f["id"]: f["text"] for f in sprinto.get("facts", [])}
    names = [r["value"] for r in rows if r["field"] == "company_name"] + [domain.split(".")[0]]
    icp = config.icp()
    meta = dict(domain=domain, rubric_version=int(rub.get("version", 1)),
                greeting_name=poc.first_name, poc=poc.model_dump_json())

    if not has_hook(rows):
        return db.save_draft(subject="", body="", sentences="[]", pain_points="[]", status="blocked",
                             check_result=CheckResult(passed=False,
                                                      failures=["no verified hook fact available"]).model_dump_json(),
                             **meta)

    pains = pick_pain_points(llm, poc, facts, config.pain_points())
    failures: list[str] | None = None
    draft, result = None, None
    for _attempt in range(2):  # first try + one regeneration with the failure reasons
        draft = llm.generate_json(build_draft_prompt(poc, facts, sprinto, pains, rub, failures),
                                  DraftOut, DRAFT_SYSTEM)
        result = check_draft(draft, poc, by_id, sp_text, qual.unknowns, names,
                             icp.get("competitors"))
        if result.passed:
            break
        failures = result.failures
    if poc.status == "needs_human_email":
        result.warnings.append("needs_human_email: no verified address; cannot be sent")
    status = "pending_review" if result.passed else "blocked"
    body = assemble_body(draft, sprinto.get("signature", ""))
    return db.save_draft(subject=draft.subject, body=body,
                         sentences=json.dumps([s.model_dump() for s in draft.sentences]),
                         pain_points=json.dumps(pains), status=status,
                         check_result=result.model_dump_json(), **meta)

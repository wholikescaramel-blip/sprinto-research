"""Go / No-Go. Rules in code over stored facts - never an LLM opinion."""
from __future__ import annotations

import re

from . import config
from .db import DB
from .models import QualResult, Reason

TRIGGER_FIELDS = [("soc2_in_progress", "SOC 2 audit in progress"),
                  ("iso27001_in_progress", "ISO 27001 in progress"),
                  ("grc_job", "Hiring for a GRC/security/compliance role"),
                  ("enterprise_signal", "Enterprise/compliance-pressure signal")]
FULL_CERT_PENALTY = 2.0  # certified (SOC 2 II + ISO) but not on a competitor tool: weaker fit


STAGE_BEYOND_PENALTY = 2.0  # known stage later than the ICP's last one (e.g. Series E vs Series A-C)


def _stage_rank(value: str) -> int | None:
    """pre-seed 0, seed 1, Series A 2, B 3, ... None if it is not a funding stage."""
    v = (value or "").casefold()
    if re.search(r"pre-?seed", v):
        return 0
    if "seed" in v:
        return 1
    m = re.search(r"series\s+([a-z])\b", v)
    return 2 + ord(m.group(1)) - ord("a") if m else None


def _ids(rows, n=2):
    return [r["id"] for r in rows[:n]]


def _nums(s: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", s.replace(",", ""))]


def qualify(db: DB, domain: str, icp_cfg: dict | None = None, rubric_cfg: dict | None = None,
            scrape_status: str | None = None) -> QualResult:
    icp = icp_cfg if icp_cfg is not None else config.icp()
    rub = rubric_cfg if rubric_cfg is not None else config.rubric()
    w = config.weights(rub) if rubric_cfg is None else {**(icp.get("weights") or {}),
                                                         **(rub.get("soft_weights") or {})}
    rv = int(rub.get("version", 1))
    F: dict[str, list] = {}
    for r in db.facts(domain):
        F.setdefault(r["field"], []).append(r)

    def res(decision, score=0.0, conf="low", reasons=None, unknowns=None):
        thr = float(icp.get("send_threshold", 3))
        return QualResult(domain=domain, decision=decision, score=score, confidence=conf,
                          reasons=reasons or [], unknowns=unknowns or [], rubric_version=rv,
                          passes_threshold=decision in ("GO", "GO_FLAGGED") and score >= thr)

    reasons: list[Reason] = []
    unknowns: list[str] = []

    # --- hard gate 6 (veto): confirmed recent negative event
    if F.get("negative_news"):
        r = F["negative_news"][0]
        return res("NO_GO", reasons=[Reason(text=f"Vetoed: recent negative news ({r['value']})",
                                            fact_ids=_ids(F["negative_news"]))])

    # --- hard gate 2 first (it is certain from the pages alone, so the scraper can stop early):
    #     fully certified AND on a listed competitor -> drop
    t2, iso, comp = F.get("cert_soc2_type2", []), F.get("cert_iso27001", []), F.get("competitor_tool", [])
    if t2 and iso and comp:
        return res("NO_GO", reasons=[Reason(
            text=f"Already fully certified (SOC 2 Type II + ISO 27001) and uses {comp[0]['value']}",
            fact_ids=_ids(t2, 1) + _ids(iso, 1) + _ids(comp, 1))])

    # --- hard gate 1: B2B SaaS (unknown = drop)
    b2b = F.get("is_b2b_saas", [])
    yes = [r for r in b2b if r["value"].lower() == "true"]
    no = [r for r in b2b if r["value"].lower() == "false"]
    if no:
        return res("NO_GO", reasons=[Reason(text="Not B2B SaaS per the company's own pages",
                                            fact_ids=_ids(no))])
    if not yes:
        incomplete = scrape_status in ("insufficient_data", "failed")
        return res("INSUFFICIENT_DATA" if incomplete else "NO_GO",
                   reasons=[Reason(text="Could not verify that this is a B2B SaaS company"
                                        + (" (fetch cap/unreachable; see missing list)" if incomplete else ""))],
                   unknowns=["is_b2b_saas"])
    reasons.append(Reason(text="B2B SaaS confirmed", fact_ids=_ids(yes)))

    score = 0.0
    cert_rows = t2 + iso + F.get("cert_soc2_type1", []) + F.get("cert_soc2", [])
    if not cert_rows:
        unknowns.append("cert_status")  # flag: assume not certified, unverified
    else:
        reasons.append(Reason(text="Existing certification noted (weaker fit if complete): "
                                   + ", ".join(sorted({r["value"] for r in cert_rows})),
                              fact_ids=_ids(cert_rows, 3)))
        if t2 and iso:
            score -= FULL_CERT_PENALTY

    # --- hard short-circuit 4: clearly out of range
    emp = (icp.get("icp") or {}).get("employees") or {"min": 20, "max": 500}
    stages = [s.casefold() for s in (icp.get("icp") or {}).get("stages", [])]
    stage_rows, hc_rows = F.get("funding_stage", []), F.get("headcount", [])
    for r in stage_rows:
        if re.search(r"public|ipo|nasdaq|nyse", r["value"], re.I):
            return res("NO_GO", reasons=[Reason(text="Public company: out of ICP range", fact_ids=[r["id"]])])
    hc_in, hc_known = None, None
    for r in hc_rows:
        nums = _nums(r["value"])
        if not nums:
            continue
        lo, hi = min(nums), max(nums)
        hc_known = hc_known or r
        if lo >= 1000:
            return res("NO_GO", reasons=[Reason(text=f"Headcount {r['value']}: far above ICP range",
                                                fact_ids=[r["id"]])])
        if any(re.search(r"seed", s["value"], re.I) for s in stage_rows) and hi < 10:
            return res("NO_GO", reasons=[Reason(text=f"Seed stage with {r['value']}: too early",
                                                fact_ids=[r["id"]] + _ids(stage_rows, 1))])
        if hi >= emp["min"] and lo <= emp["max"]:
            hc_in = hc_in or r

    # --- soft signals
    trig = [(label, F[f][0]) for f, label in TRIGGER_FIELDS if F.get(f)]
    if trig:
        score += float(w.get("compliance_trigger", 3))
        for label, row in trig:
            reasons.append(Reason(text=f"Compliance trigger: {label}", fact_ids=[row["id"]]))
    else:
        unknowns.append("compliance_trigger")

    # A company is at its MOST ADVANCED stage: facts can mention several ("Series A through D", a news raise of
    # Series E). Judging by any one fact let Cohere (Series E) count as "Series A".
    icp_ranks = {rk for s in stages if (rk := _stage_rank(s)) is not None}
    ranked = [(rk, r) for r in stage_rows if (rk := _stage_rank(r["value"])) is not None]
    top = max(ranked, key=lambda x: x[0]) if ranked else None
    stage_hit = top[1] if top and top[0] in icp_ranks else None
    beyond = bool(top and icp_ranks and top[0] > max(icp_ranks))   # e.g. Series D/E when the ICP is Series A-C
    if beyond:
        score -= float(w.get("stage_beyond_penalty", STAGE_BEYOND_PENALTY))
        reasons.append(Reason(text=f"Stage is beyond the ICP ({top[1]['value']}): usually past the point of "
                                   "buying a first compliance program", fact_ids=[top[1]["id"]]))
    if stage_hit or hc_in:
        score += float(w.get("stage_fit", 2))
        if stage_hit:
            reasons.append(Reason(text=f"Stage in ICP: {stage_hit['value']}", fact_ids=[stage_hit["id"]]))
        if hc_in:
            reasons.append(Reason(text=f"Headcount in ICP range: {hc_in['value']}", fact_ids=[hc_in["id"]]))
    elif beyond:
        pass  # already explained above
    elif stage_rows or hc_known:
        reasons.append(Reason(text="Stage/headcount known but outside ICP range (no stage points)",
                              fact_ids=_ids(stage_rows, 1) + ([hc_known["id"]] if hc_known else [])))
    else:
        unknowns.append("stage_fit")

    if F.get("eu_regulated"):
        score += float(w.get("eu_regulated", 1))
        reasons.append(Reason(text=f"EU/regulated signal: {F['eu_regulated'][0]['value']}",
                              fact_ids=_ids(F["eu_regulated"], 1)))
    # row 5 unanswered -> ignore (bonus only)

    decision = "GO_FLAGGED" if unknowns else "GO"
    conf = "high" if not unknowns else ("medium" if len(unknowns) == 1 else "low")
    return res(decision, score, conf, reasons, unknowns)

"""PoC finder: Hunter (cached, quota-counted) -> manual Apollo file -> company page -> generic inbox.

email_status values:
  verified   - Hunter verification.status == "valid", or a human-supplied (Apollo manual) address
  sourced    - Hunter returned it with provenance "found" and at least one source page
  unverified - anything else (named person without email, pattern guess). NEVER auto-sent.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import config
from .db import DB
from .models import Poc

HUNTER_URL = "https://api.hunter.io/v2/domain-search"
ROLE_PATTERNS = {
    "security_head": r"\bCISO\b|chief information security|head of (?:information )?security|"
                     r"vp,? (?:of )?security|director,? (?:of )?(?:information )?security|"
                     r"security (?:lead|head|officer)|information security",
    "compliance_grc": r"compliance|\bGRC\b|governance|risk",
    "cto_vp_eng": r"\bCTO\b|chief technology|vp,? (?:of )?eng|head of engineering|"
                  r"engineering (?:lead|director)|director,? (?:of )?engineering",
    "founder_ceo": r"founder|\bCEO\b|chief executive",
}
ROLE_KEYS = {"security": "security_head", "compliance": "compliance_grc",
             "engineering": "cto_vp_eng", "founder": "founder_ceo"}


def classify_role(title: str | None) -> str | None:
    for key, rx in ROLE_PATTERNS.items():
        if title and re.search(rx, title, re.I):
            return key
    return None


def _rank(role: str | None, priority: list[str]) -> int:
    order = [ROLE_KEYS.get(p, p) for p in priority]
    return order.index(role) if role in order else 99


def guess_email(name: str | None, domain: str, pattern: str | None) -> str | None:
    """Suggestion for a human only. Never used as `email`, never sent."""
    if not name or not pattern or len(name.split()) < 2:
        return None
    first, last = name.split()[0].lower(), name.split()[-1].lower()
    local = (pattern.replace("{first}", first).replace("{last}", last)
             .replace("{f}", first[0]).replace("{l}", last[0]))
    return f"{local}@{domain}" if "{" not in local else None


def hunter_search(db: DB, domain: str, api_key: str | None, monthly_limit: int = 25,
                  client: httpx.Client | None = None) -> dict | None:
    """Cached forever per domain; one call max; respects the monthly quota counter."""
    cached = db.hunter_cached(domain)
    if cached is not None:
        return cached
    if not api_key:
        return None
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    if db.counter("hunter", period) >= monthly_limit:
        return None
    c = client or httpx.Client(timeout=20)
    params = {"domain": domain, "api_key": api_key, "limit": 10, "type": "personal",
              "seniority": "senior,executive", "required_field": "position"}
    db.bump_counter("hunter", period)
    try:
        r = c.get(HUNTER_URL, params=params)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:  # 401/403/429/...: do not cache failures
        return None
    data = r.json()
    db.hunter_store(domain, data)
    return data


def _hunter_candidates(resp: dict) -> list[dict]:
    out = []
    for e in ((resp or {}).get("data") or {}).get("emails") or []:
        first, last = e.get("first_name"), e.get("last_name")
        if not (e.get("value") and first and last and e.get("position")):
            continue
        ver = (e.get("verification") or {}).get("status")
        if ver == "valid":
            es = "verified"
        elif e.get("provenance") == "found" and e.get("sources"):
            es = "sourced"
        else:
            es = "unverified"
        out.append({"name": f"{first} {last}", "title": e["position"], "email": e["value"],
                    "confidence": e.get("confidence"), "verification": ver, "email_status": es})
    return out


def find_poc(db: DB, domain: str, api_key: str | None = None, icp_cfg: dict | None = None,
             manual: dict | None = None, client: httpx.Client | None = None) -> Poc:
    icp = icp_cfg if icp_cfg is not None else config.icp()
    pcfg = icp.get("poc") or {}
    priority = pcfg.get("role_priority") or ["security", "compliance", "engineering", "founder"]
    founder_max = int(pcfg.get("founder_max_employees", 50))
    manual = manual if manual is not None else config.manual_pocs()

    # headcount (for the "founder only at small companies" rule)
    hc = None
    for r in db.facts(domain, "headcount"):
        nums = [int(x) for x in re.findall(r"\d+", r["value"].replace(",", ""))]
        if nums:
            hc = max(nums)
            break

    def role_ok(role):
        if role is None:
            return False
        return role != "founder_ceo" or hc is None or hc <= founder_max

    # 1. Hunter
    resp = hunter_search(db, domain, api_key, int(pcfg.get("hunter_monthly_limit", 25)), client)
    if resp:
        cands = []
        for c in _hunter_candidates(resp):
            c["role"] = classify_role(c["title"])
            if role_ok(c["role"]):
                cands.append(c)
        cands.sort(key=lambda c: (_rank(c["role"], priority), -(c["confidence"] or 0)))
        if cands:
            c = cands[0]
            return Poc(domain=domain, status="ok", name=c["name"], title=c["title"], email=c["email"],
                       email_status=c["email_status"], source="hunter", confidence=c["confidence"],
                       verification=c["verification"], role_type=c["role"],
                       reason=f"Hunter: best role match by priority {priority} "
                              f"({c['role']}; confidence {c['confidence']}; verification {c['verification']})")

    # 2. Fallback A: manual Apollo file
    m = (manual or {}).get(domain)
    if m and m.get("name") and m.get("email"):
        role = classify_role(m.get("title"))
        return Poc(domain=domain, status="ok", name=m["name"], title=m.get("title"), email=m["email"],
                   email_status=m.get("email_status", "verified"), source=m.get("source", "apollo_manual"),
                   role_type=role, reason="Manual Apollo lookup from config/manual_pocs.yaml")

    # 3. Fallback B: named person + title on the company's own pages (no email)
    people = []
    for r in db.facts(domain, "person"):
        name, _, title = r["value"].partition(" | ")
        role = classify_role(title)
        if role_ok(role):
            people.append((_rank(role, priority), name, title, role, r))
    people.sort(key=lambda p: p[0])
    if people:
        _, name, title, role, r = people[0]
        pattern = (((resp or {}).get("data") or {}).get("pattern"))
        return Poc(domain=domain, status="needs_human_email", name=name, title=title, email=None,
                   email_status="unverified", source="company_page", role_type=role,
                   guessed_email=guess_email(name, domain, pattern),
                   reason=f"Named on the company's own page (fact {r['id']}); no email found. "
                          f"A human must supply a verified address.")

    # 4. Last resort: generic inbox only
    g = db.facts(domain, "generic_inbox")
    return Poc(domain=domain, status="no_named_poc", email=g[0]["value"] if g else None,
               email_status="unverified", source="generic_inbox" if g else None,
               reason="No named decision-maker found; hand to a human.")

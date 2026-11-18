"""Claim checker: code, not an LLM. Runs before the review gate.

Blocks a draft when any sentence makes a claim the cited facts do not support.
Deliberately conservative: a false block costs one regeneration; a false pass costs credibility.
"""
from __future__ import annotations

import re
from typing import Mapping

from .models import CheckResult, DraftOut, Poc

CERT_TERMS = [r"soc\s?2", r"iso\s?-?27001", r"gdpr", r"hipaa", r"pci", r"type\s?(?:i{1,2}|1|2)\b"]
FUND_TERMS = [r"series\s+[a-e]\b", r"\bseed\b", r"\braised\b", r"\bfunding\b", r"\bfunded\b", r"\$\s?\d"]
HIRE_TERMS = [r"\bhiring\b", r"\bopen role", r"\bjob\b", r"\bposition\b", r"\brecruit", r"\bcareers? page"]
JOB_FIELDS = {"grc_job"}
CLAIM_WORDS_SPRINTO = re.compile(r"\d|%|\bcustomers?\b|trusted by|\bleading\b|fastest|\bbest\b|#1", re.I)

# proper nouns that never need a source
ALLOWED_CAPS = {"SOC", "ISO", "GDPR", "HIPAA", "PCI", "DSS", "GRC", "CISO", "CTO", "CEO", "VP", "IT",
                "SaaS", "B2B", "AI", "Type", "Sprinto", "II", "Q1", "Q2", "Q3", "Q4"}

# an unknown signal must never leak into the email
UNKNOWN_TERMS = {
    "stage_fit": r"\bseries\s+[a-e]\b|\bseed\b|\bfunding\b|\braised\b|\bheadcount\b|\bemployees\b|\bteam of\b",
    "compliance_trigger": r"\bhiring\b|\baudit\b|\bsoc\s?2\b|\biso\s?27001\b|\bquestionnaire|\benterprise (?:customer|deal|buyer)",
    "cert_status": r"\bcertified\b|\bcertification\b|\balready (?:have|hold)\b|\bcompliant\b",
    "is_b2b_saas": r"\bb2b\b|\bsaas\b",
}


def _digits(s: str) -> list[str]:
    return [d.replace(",", "") for d in re.findall(r"\d[\d,]*", s)]


def unsupported_claims(text: str, support_rows: list[Mapping], extra_support: str = "") -> list[str]:
    """Claim-like tokens in `text` that the cited rows (snippet/value/field) do not back."""
    support = (" ".join(f"{r['snippet']} {r['value']}" for r in support_rows) + " " + extra_support).casefold()
    fields = {r["field"] for r in support_rows}
    bad: list[str] = []
    for rx in CERT_TERMS:
        if re.search(rx, text, re.I) and not re.search(rx, support, re.I):
            bad.append(f"certification claim '{re.search(rx, text, re.I).group(0)}'")
    for rx in FUND_TERMS:
        m = re.search(rx, text, re.I)
        if m and not (re.search(rx, support, re.I) or "funding_stage" in fields):
            bad.append(f"funding claim '{m.group(0)}'")
    for rx in HIRE_TERMS:
        m = re.search(rx, text, re.I)
        if m and not (fields & JOB_FIELDS or re.search(rx, support, re.I)):
            bad.append(f"hiring claim '{m.group(0)}'")
    scrubbed = text
    for rx in CERT_TERMS:  # "SOC 2" / "ISO 27001" digits are covered by the cert check above
        scrubbed = re.sub(rx, " ", scrubbed, flags=re.I)
    sup_nums = set(_digits(support))
    for n in _digits(scrubbed):
        if n not in sup_nums:
            bad.append(f"number '{n}'")
    words = re.findall(r"[A-Za-z0-9&']+", text)
    for i, w in enumerate(words):
        core = w.strip("'")
        if i == 0 or len(core) < 2 or not core[0].isupper() or core in ALLOWED_CAPS or core.upper() in ALLOWED_CAPS:
            continue
        if core.casefold() not in support and core.casefold().rstrip("s") not in support:
            bad.append(f"name/term '{core}'")
    return bad


def check_draft(draft: DraftOut, poc: Poc, facts: Mapping[str, Mapping], sprinto: Mapping[str, str],
                unknowns: list[str], company_names: list[str], competitors: list[str] | None = None
                ) -> CheckResult:
    """facts: company fact rows by id. sprinto: {S-id: text}. company_names: allowed proper nouns."""
    fails: list[str] = []
    warns: list[str] = []
    extra = " ".join(company_names + [poc.name or "", poc.title or ""])
    types = [s.type for s in draft.sentences]

    # recipient must match the stored PoC
    if not poc.first_name or draft.greeting_name.strip().casefold() != poc.first_name.casefold():
        fails.append(f"recipient name '{draft.greeting_name}' does not match stored PoC '{poc.first_name}'")
    if "hook" not in types:
        fails.append("no hook sentence")
    if "pain" not in types:
        fails.append("no pain-point sentence")
    if "cta" not in types:
        warns.append("no call-to-action sentence")

    all_cited: list[Mapping] = []
    for i, s in enumerate(draft.sentences, 1):
        tag = f"sentence {i} ({s.type})"
        co_ids = [x for x in s.fact_ids if not x.startswith("S")]
        sp_ids = [x for x in s.fact_ids if x.startswith("S")]
        missing = [x for x in co_ids if x not in facts]
        bad_sp = [x for x in sp_ids if x not in sprinto]
        if missing:
            fails.append(f"{tag}: cites unknown fact id(s) {missing}")
        if bad_sp:
            fails.append(f"{tag}: cites unknown Sprinto fact id(s) {bad_sp}")
        rows = [facts[x] for x in co_ids if x in facts]
        all_cited += rows
        mentions_sprinto = bool(re.search(r"\bsprinto\b", s.text, re.I))

        if s.type in ("hook", "pain") and not co_ids:
            fails.append(f"{tag}: no company fact id cited")
        if s.type == "pitch" or (mentions_sprinto and s.type != "cta"):
            if not sp_ids:
                fails.append(f"{tag}: claim about Sprinto without a sprinto_facts id")
            else:
                sup = " ".join(sprinto.get(x, "") for x in sp_ids)
                bad = unsupported_claims(re.sub(r"\bsprinto\b", "", s.text, flags=re.I),
                                         [{"snippet": sup, "value": "", "field": "sprinto"}], extra)
                if bad:
                    fails.append(f"{tag}: Sprinto claim not in sprinto_facts: {bad}")
        elif s.type == "cta" and mentions_sprinto and CLAIM_WORDS_SPRINTO.search(s.text) and not sp_ids:
            fails.append(f"{tag}: CTA makes a Sprinto claim without a sprinto_facts id")
        else:
            bad = unsupported_claims(s.text, rows, extra)
            if bad:
                fails.append(f"{tag}: unsupported claim(s) {bad}")
            for u in unknowns:
                rx = UNKNOWN_TERMS.get(u)
                if rx and re.search(rx, s.text, re.I):
                    fails.append(f"{tag}: mentions '{u}', which we could not verify")
        for c in competitors or []:
            if re.search(r"\b" + re.escape(c) + r"\b", s.text, re.I):
                fails.append(f"{tag}: names competitor {c}")

    # subject line: same rules, against every cited fact
    sub_bad = unsupported_claims(draft.subject, all_cited, extra)
    if sub_bad:
        fails.append(f"subject: unsupported claim(s) {sub_bad}")
    for u in unknowns:
        rx = UNKNOWN_TERMS.get(u)
        if rx and re.search(rx, draft.subject, re.I):
            fails.append(f"subject: mentions '{u}', which we could not verify")
    if len(draft.subject.split()) >= 8:
        warns.append("subject is 8+ words")
    wc = sum(len(s.text.split()) for s in draft.sentences)
    if not 70 <= wc <= 130:
        warns.append(f"body is {wc} words (target 80-120)")
    return CheckResult(passed=not fails, failures=fails, warnings=warns)

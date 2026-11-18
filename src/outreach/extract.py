"""Fact extraction. Rule: copy from fetched text or store nothing.

Two sources of facts, both stored as {id, field, value, source_url, snippet, fetched_at}:
  1. deterministic regex extractors (certs, jobs, triggers, generic inboxes, GDPR...)
  2. ONE Gemini call per company, whose every field is checked in code: the snippet must be a
     substring of the cited page's text, else the field is discarded (-> null).
A missing fact is simply absent. Nothing is ever inferred.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .db import DB
from .llm import LLMClient
from .models import ExtractionOut, FieldOut
from .scraper import ATS_HOSTS
from .util import norm_ws, snippet_in_text

PROGRESS = re.compile(r"in progress|working (?:toward|towards|on)|preparing|pursuing|preparation|"
                      r"undergoing|underway|planning|will be|soon|coming|currently|on track|"
                      r"getting ready|ready for", re.I)
GENERIC_LOCALS = {"info", "sales", "hello", "contact", "support", "team", "hi", "press", "marketing",
                  "admin", "careers", "jobs", "help", "office", "mail", "enquiries", "inquiries"}
JOB_ROLE = re.compile(r"\bGRC\b|governance,? risk|compliance|information security|security "
                      r"(?:engineer|analyst|manager|lead|officer|architect|program)|\bCISO\b|"
                      r"head of security|privacy (?:manager|counsel|officer|lead)|risk (?:manager|analyst)|"
                      r"trust (?:&|and) (?:safety|security)", re.I)


def is_jobs_url(url: str) -> bool:
    p = urlparse(url)
    h = p.netloc.lower()
    return bool(re.search(r"/careers?|/jobs", p.path, re.I)) or any(
        h == a or h.endswith("." + a) for a in ATS_HOSTS)


def snippet_around(text: str, start: int, end: int, maxlen: int = 300) -> str:
    """Sentence/line containing [start,end), sliced straight from text (so it is a substring)."""
    lo = max(text.rfind("\n", 0, start), text.rfind(". ", 0, start)) + 1
    hits = [i for i in (text.find("\n", end), text.find(". ", end)) if i >= 0]
    hi = (min(hits) + 1) if hits else len(text)
    s = text[lo:hi].strip()
    if len(s) > maxlen:
        a = max(0, start - lo - maxlen // 2)
        s = text[lo + a: lo + a + maxlen].strip()
    return s


def deterministic_facts(db: DB, domain: str, competitors: list[str]) -> int:
    """Run all regex extractors over cached pages. Idempotent (fact ids are content hashes)."""
    n = 0
    comp_rx = re.compile(r"\b(" + "|".join(map(re.escape, competitors)) + r")\b", re.I) if competitors else None
    patterns = [
        ("soc2", re.compile(r"SOC\s?2(?:\s*\(?\s*Type\s*(I{1,2}|1|2)\)?)?", re.I)),
        ("iso27001", re.compile(r"ISO[\s/]*(?:IEC\s*)?27001", re.I)),
    ]
    for pg in db.pages_for(domain):
        url, text, pid = pg["url"], pg["text"], pg["id"]
        if not text:
            continue

        def add(field, value, start, end=None, snippet=None):
            nonlocal n
            snip = snippet or snippet_around(text, start, end if end is not None else start)
            db.add_fact(domain, field, value, url, snip, pid)
            n += 1

        for kind, rx in patterns:
            for m in rx.finditer(text):
                snip = snippet_around(text, m.start(), m.end())
                in_progress = bool(PROGRESS.search(snip))
                if kind == "soc2":
                    t = (m.group(1) or "").upper()
                    if in_progress:
                        add("soc2_in_progress", "SOC 2 in progress", m.start(), m.end())
                    elif t in ("II", "2"):
                        add("cert_soc2_type2", "SOC 2 Type II", m.start(), m.end())
                    elif t in ("I", "1"):
                        add("cert_soc2_type1", "SOC 2 Type I", m.start(), m.end())
                    else:
                        add("cert_soc2", "SOC 2 (type not stated)", m.start(), m.end())
                else:
                    if in_progress:
                        add("iso27001_in_progress", "ISO 27001 in progress", m.start(), m.end())
                    else:
                        add("cert_iso27001", "ISO 27001", m.start(), m.end())
        if comp_rx:
            for m in comp_rx.finditer(text):
                add("competitor_tool", m.group(1).title(), m.start(), m.end())
        if is_jobs_url(url):
            seen = 0
            for line in text.split("\n"):
                l = line.strip()
                if 3 < len(l) <= 100 and not l.endswith(".") and JOB_ROLE.search(l) and seen < 3:
                    add("grc_job", l, 0, snippet=l)
                    seen += 1
        for rx, label in [(r"security questionnaires?", "security questionnaire"),
                          (r"data processing (?:agreement|addendum)|\bDPA\b", "DPA"),
                          (r"\bFortune\s?(?:100|500|1000)\b", "Fortune-scale customers"),
                          (r"trust cent(?:er|re)", "trust center")]:
            m = re.search(rx, text, re.I)
            if m:
                add("enterprise_signal", label, m.start(), m.end())
        if re.search(r"/pricing|/plans", url):
            m = re.search(r"^\s*Enterprise\b.*$", text, re.M)
            if m:
                add("enterprise_signal", "Enterprise pricing tier", m.start(), m.end())
        for m in re.finditer(r"\bGDPR\b|\bHIPAA\b|\bPCI[- ]DSS\b", text):
            add("eu_regulated", m.group(0), m.start(), m.end())
            break
        for m in re.finditer(r"Series\s+[A-E]\b", text):
            snip = snippet_around(text, m.start(), m.end())
            if re.search(r"rais|funding|led by|round|backed|investor", snip, re.I):
                add("funding_stage", m.group(0), m.start(), m.end())
                break
        for m in re.finditer(r"(\d[\d,]*)\+?\s+(?:employees|team members|people)\b", text, re.I):
            add("headcount", m.group(0), m.start(), m.end())
            break
        for e in _j(pg["raw_emails"]):
            local, _, host = e.partition("@")
            if local in GENERIC_LOCALS and host.endswith(domain):
                add("generic_inbox", e, 0, snippet=e)
    return n


def _j(s):
    import json
    return json.loads(s) if s else []


# --------------------------------------------------------------------------- LLM extraction
SYSTEM = ("You extract facts from web page text for sales research. Copy ONLY what the text states. "
          "If the text does not say it, return null for that field. Never infer, guess, or use outside "
          "knowledge. Every non-null field needs source_url (exactly as in the page header) and a "
          "snippet copied VERBATIM from that page's text.")

PROMPT = """Extract these fields from the pages below.
- company_name: the company's name.
- product_description: one sentence copied/lightly trimmed from the text describing what the product does.
- is_b2b_saas: value "true" only if the text states a software product sold to businesses (demo, pricing per seat, API docs, login for teams). Value "false" only if the text clearly shows a consumer or non-software business. Otherwise null.
- funding_stage: e.g. "Series B", only if the text says it.
- headcount: employee count or range, only if the text says it.
- eu_or_regulated_customers: only if the text names EU/regulated customers or offices.
- named_customers: customer names the text lists (value = the name).
- people: named people WITH their title as printed on the page (security/compliance/engineering leaders and founders especially).
Return null / [] when absent.

{pages}"""


def build_pages_block(pages, per_page: int = 6000) -> str:
    return "\n\n".join(f"=== PAGE: {p['url']} ===\n{p['text'][:per_page]}" for p in pages)


def _digits_ok(f: FieldOut) -> bool:
    snip = norm_ws(f.snippet).replace(",", "")
    nums = re.findall(r"\d+", f.value.replace(",", ""))
    return bool(nums) and all(n in snip for n in nums)


def _ok(f: FieldOut | None, texts: dict[str, str]) -> bool:
    return bool(f and f.value and f.source_url in texts and snippet_in_text(f.snippet, texts[f.source_url]))


def llm_extract(db: DB, domain: str, llm: LLMClient) -> dict:
    """The single LLM call for this company. Returns {'kept': n, 'discarded': [...]}."""
    pages = [p for p in db.pages_for(domain) if p["text"]]
    if not pages:
        return {"kept": 0, "discarded": ["no pages"]}
    texts = {p["url"]: p["text"] for p in pages}
    pid = {p["url"]: p["id"] for p in pages}
    out = llm.generate_json(PROMPT.format(pages=build_pages_block(pages)), ExtractionOut, SYSTEM)
    kept, dropped = 0, []

    def keep(field: str, f: FieldOut | None, extra_check=None):
        nonlocal kept
        if f is None or f.value is None:
            return
        if not _ok(f, texts) or (extra_check and not extra_check(f)):
            dropped.append(f"{field}: snippet/value not supported by source text")
            return
        db.add_fact(domain, field, f.value.strip(), f.source_url, norm_ws(f.snippet), pid[f.source_url])
        kept += 1

    keep("company_name", out.company_name)
    keep("product_description", out.product_description)
    keep("is_b2b_saas", out.is_b2b_saas, lambda f: f.value.strip().lower() in ("true", "false"))
    keep("funding_stage", out.funding_stage,
         lambda f: f.value.casefold() in norm_ws(f.snippet).casefold())
    keep("headcount", out.headcount, _digits_ok)
    keep("eu_regulated", out.eu_or_regulated_customers)
    for c in out.named_customers:
        keep("named_customer", c, lambda f: f.value.casefold() in norm_ws(f.snippet).casefold())
    for p in out.people:
        f = FieldOut(value=f"{p.name} | {p.title}", source_url=p.source_url, snippet=p.snippet)
        keep("person", f, lambda f, p=p: p.name.casefold() in norm_ws(p.snippet).casefold()
             and p.title.casefold() in norm_ws(p.snippet).casefold())
    return {"kept": kept, "discarded": dropped}

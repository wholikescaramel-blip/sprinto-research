"""Stage orchestration. Every stage reads/writes SQLite; failures are per-company and soft."""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field

from . import config
from .db import DB, now
from .discovery import cheap_filter, dedupe, discover, is_parked
from .draft import make_draft
from .extract import deterministic_facts, llm_extract
from .llm import LLMClient
from .news import check_negative_news, default_fetch, gap_fill_funding, make_judge
from .poc import find_poc
from .qualify import qualify
from .scraper import Fetcher, scrape_company


@dataclass
class Services:
    db: DB
    llm: LLMClient | None = None
    fetcher: Fetcher | None = None
    news_fetch: object = default_fetch
    news_judge: object = None   # (domain, events) -> indices about THIS company; default: Gemini picks from the real headlines
    hunter_client: object = None
    resolver: object = None
    cfg: dict = field(default_factory=config.icp)

    def get_fetcher(self) -> Fetcher:
        if self.fetcher is None:
            lim = self.cfg.get("limits") or {}
            budget = float(lim.get("scrape_time_budget_seconds", 30) or 0)
            self.fetcher = Fetcher(self.db, max_fetches=int(lim.get("max_fetches_per_company", 8)),
                                   delay=float(lim.get("delay_seconds", 1.0)),
                                   timeout=float(lim.get("request_timeout_seconds", 8)),
                                   time_budget=budget or None)
        return self.fetcher


def _company_name(db: DB, domain: str) -> str:
    rows = db.facts(domain, "company_name")
    if rows:
        return rows[0]["value"]
    c = db.one("SELECT name FROM companies WHERE domain=?", (domain,))
    return (c["name"] if c and c["name"] else domain.split(".")[0]).strip()


def _aliases(db: DB, domain: str) -> list[str]:
    """Names to match in headlines. Before scraping we only know the given name and the domain."""
    c = db.one("SELECT name FROM companies WHERE domain=?", (domain,))
    stem = domain.split(".")[0]
    out: list[str] = []
    for a in (c["name"] if c and c["name"] else None, stem, stem.replace("-", " ")):
        if a and a.casefold() not in [x.casefold() for x in out]:
            out.append(a)
    return out


def research(svc: Services, domain: str, log=None) -> dict:
    """Order (per mentor): 1) Google News + RSS: bad-news veto and a funding lookup, 2) our own scraper,
    3) deterministic facts, 4) ONE Gemini extraction. A confirmed veto skips scraping entirely."""
    db, cfg = svc.db, svc.cfg
    log = log or (lambda m: None)
    db.upsert_company(domain)
    comps = cfg.get("competitors") or []
    reason = cheap_filter(domain, svc.resolver) if svc.resolver else cheap_filter(domain)
    if reason:
        db.set_stage(domain, "scrape", "failed", f"dropped: {reason}")
        return {"domain": domain, "dropped": reason}

    # ---- step 1: Google News RSS + configured RSS (before any page of the company is fetched)
    log(f"stage:news|News + RSS check for {domain}")
    aliases = _aliases(db, domain)
    # news is re-fetched on every run, so drop old news facts first (a stale veto must not outlive the rule that made it)
    db.x("DELETE FROM facts WHERE domain=? AND field IN ('negative_news','news_unverified')", (domain,))
    lookback = int((cfg.get("news") or {}).get("lookback_days", 365))
    judge = svc.news_judge or (make_judge(svc.llm) if svc.llm is not None else None)
    check_negative_news(db, domain, aliases, lookback, (cfg.get("discovery") or {}).get("rss_feeds") or [],
                        svc.news_fetch, judge=judge)
    if db.facts(domain, "negative_news"):
        db.set_stage(domain, "news", "vetoed", "recent negative news")
        db.set_stage(domain, "scrape", "skipped", "vetoed by recent negative news; nothing fetched")
        return {"domain": domain, "status": "vetoed"}
    gap_fill_funding(db, domain, aliases, svc.news_fetch)  # one attempt; stage is unknown before scraping
    db.set_stage(domain, "news", "done")

    # ---- step 2: our own scraper does the heavy lifting
    log(f"stage:scrape|Researching {domain}")

    def answered(cat: str) -> bool:
        deterministic_facts(db, domain, comps)
        have = {r["field"] for r in db.facts(domain)}
        return {"trust": bool(have & {"cert_soc2_type2", "cert_soc2_type1", "cert_soc2", "cert_iso27001"}),
                "customers": bool(have & {"soc2_in_progress", "grc_job", "enterprise_signal"}),
                "ats": "grc_job" in have, "privacy": "eu_regulated" in have,
                "contact": "generic_inbox" in have}.get(cat, False)

    def certain_no_go() -> bool:  # fully certified AND on a competitor tool: proven from the pages alone
        have = {r["field"] for r in db.facts(domain)}
        return {"cert_soc2_type2", "cert_iso27001", "competitor_tool"} <= have

    rep = scrape_company(db, domain, svc.get_fetcher(), answered, stop=certain_no_go)
    deterministic_facts(db, domain, comps)
    pages = db.pages_for(domain)
    if not rep.home_ok:
        db.set_stage(domain, "scrape", "failed", "homepage unreachable or blocked")
        return {"domain": domain, "status": "failed"}
    if is_parked(" ".join(p["text"] for p in pages)):
        db.set_stage(domain, "scrape", "failed", "dropped: parked domain")
        return {"domain": domain, "dropped": "parked domain"}

    info = {"kept": 0, "discarded": []}
    max_llm = int((cfg.get("limits") or {}).get("max_llm_extractions_per_company", 1))
    if svc.llm is not None and max_llm >= 1 and not rep.early_stop:  # no extraction call for a proven NO_GO
        info = llm_extract(db, domain, svc.llm)
    db.set_stage(domain, "extract", "done", json.dumps(info))

    have = {r["field"] for r in db.facts(domain)}
    missing = [f for f in ("is_b2b_saas", "funding_stage", "headcount") if f not in have]
    if not have & {"cert_soc2_type2", "cert_soc2_type1", "cert_soc2", "cert_iso27001"}:
        missing.append("cert_status")
    if not have & {"soc2_in_progress", "grc_job", "enterprise_signal"}:
        missing.append("compliance_trigger")
    # Never guess: if the cap or the clock stopped us and we still cannot verify B2B SaaS, say so.
    # Cached pages are free, so a re-run picks up where this one stopped.
    incomplete = rep.cap_hit or rep.time_hit
    status = "insufficient_data" if incomplete and "is_b2b_saas" in missing else "done"
    db.set_stage(domain, "scrape", status, json.dumps({
        "missing": missing, "fetched": rep.fetched, "cap_hit": rep.cap_hit, "time_hit": rep.time_hit,
        "elapsed_seconds": rep.elapsed, "skipped_for_time": rep.skipped}))
    return {"domain": domain, "status": status, "missing": missing, "facts": len(db.facts(domain)),
            "elapsed": rep.elapsed, "time_hit": rep.time_hit}


def qualify_company(svc: Services, domain: str):
    q = qualify(svc.db, domain, svc.cfg, scrape_status=svc.db.stage(domain, "scrape"))
    svc.db.save_qual(q)
    svc.db.set_stage(domain, "qualify", "done")
    if not q.passes_threshold:  # no longer a good fit: retire its unsent drafts (approved/sent ones are left alone)
        svc.db.x("UPDATE drafts SET status='superseded', updated_at=? WHERE domain=? "
                 "AND status IN ('pending_review','blocked')", (now(), domain))
    return q


def poc_company(svc: Services, domain: str):
    poc = find_poc(svc.db, domain, config.env("HUNTER_API_KEY"), svc.cfg, client=svc.hunter_client)
    svc.db.save_poc(poc)
    svc.db.set_stage(domain, "poc", "done")
    return poc


def draft_company(svc: Services, domain: str):
    did = make_draft(svc.db, domain, svc.llm)
    svc.db.set_stage(domain, "draft", "done" if did else "skipped",
                     None if did else "no named PoC; hand to a human")
    return did


def run_pipeline(svc: Services, domains: list[str], do_discover: bool = False,
                 limit: int | None = None, names: list[str] | None = None,
                 min_companies: int = 3, progress=None) -> dict:
    """Inputs: manual domains and/or company names. With NO manual input (or --discover), the system
    finds companies itself: configured RSS/directory sources, then ICP search. Every discovered
    domain is verified in code (homepage must name the company) before it is researched."""
    db = svc.db
    names = names or []
    log = progress or (lambda m: None)
    log('stage:discover|Looking for companies')
    run_id = db.x("INSERT INTO runs(args,started_at,created_at,updated_at) VALUES(?,?,?,?)",
                  (json.dumps({"domains": len(domains), "names": len(names), "discover": do_discover,
                               "limit": limit, "min_companies": min_companies}), now(), now(), now()))
    import socket
    resolver = svc.resolver or socket.gethostbyname
    notes: list[str] = []
    manual = list(domains)
    if names and svc.llm is not None:
        try:
            from .discovery import resolve_names
            got = resolve_names(db, svc.llm, svc.get_fetcher(), names, resolver, notes=notes)
            manual += [g["domain"] for g in got]
        except Exception as e:
            notes.append(f"name resolution failed softly: {e}")
    manual = dedupe(manual)
    if limit:
        manual = manual[:limit]
    discovering = (do_discover or not (domains or names)) and svc.llm is not None
    target = max(1, min_companies)          # how many GOOD-FIT companies we want, not how many we look at
    max_candidates = target * 4             # never research more than this many to find them
    summary = {"companies": 0, "errors": {}, "qualified": [], "drafts": [], "notes": notes}
    good: list = []                         # qualified: GO / GO_FLAGGED at or above the send threshold
    seen: set[str] = set()

    def process(d: str) -> None:
        """Research -> qualify one company; if it is a good fit, find a contact and draft right away."""
        if d in seen:
            return
        seen.add(d)
        summary["companies"] += 1
        try:
            r = research(svc, d, log)
            if r.get("dropped") or r.get("status") == "failed":
                summary["errors"][d] = r.get("dropped") or "homepage unreachable"
                return
            log(f'stage:qualify|Qualifying {d}')
            q = qualify_company(svc, d)
            log(f'done:{d}|{d}: {q.decision} (score {q.score})')
        except Exception as e:  # fail soft per company
            db.set_stage(d, "research", "failed", f"{e}\n{traceback.format_exc(limit=3)}")
            summary["errors"][d] = str(e)
            return
        if not q.passes_threshold:
            return
        good.append(q)
        try:
            log(f'stage:poc|Finding a contact at {d}')
            poc = poc_company(svc, d)
            log(f'stage:draft|Drafting for {d}')
            did = draft_company(svc, d) if svc.llm is not None else None
            summary["drafts"].append((d, poc.status, did))
        except Exception as e:
            db.set_stage(d, "draft", "failed", str(e))
            summary["errors"][d] = f"drafting failed: {e}"
            notes.append(f"{d} qualified but its draft failed ({str(e)[:90]}). Use 'Draft email' on its card to retry.")

    if manual:
        log(f'found:{len(manual)}|{len(manual)} companies to research')
        for d in manual:
            process(d)

    if discovering:
        # Discovery is lazy: look for candidates a few at a time and STOP as soon as we have `target` good fits.
        # Companies that show no sign of B2B software on their homepage are never researched at all.
        from .discovery import default_queries, icp_search
        try:
            for d in [x["domain"] for x in discover(db, svc.llm, svc.get_fetcher(), svc.cfg)]:
                if len(good) >= target or len(seen) >= max_candidates:
                    break
                process(d)
        except Exception as e:
            notes.append(f"configured-source discovery failed softly: {e}")
        for qi in range(len(default_queries(svc.cfg))):
            if len(good) >= target or len(seen) >= max_candidates:
                break
            log(f'stage:discover|Searching for candidates (query {qi + 1})')
            try:
                leads = icp_search(db, svc.llm, svc.get_fetcher(), svc.cfg, target=8, only_query=qi,
                                   max_verify=6, resolver=resolver, notes=notes, b2b_gate=True)
            except Exception as e:
                notes.append(f"ICP search failed softly: {e}")
                break
            for lead in leads:
                if len(good) >= target or len(seen) >= max_candidates:
                    break
                process(lead["domain"])
    for n in notes:  # say WHY when discovery came up short, instead of just "0 companies"
        log(f"note|{n}")
    summary["qualified"] = sorted(((q.domain, q.decision, q.score) for q in good), key=lambda x: -x[2])
    log('stage:review|Ready for review')
    if len(good) < target:
        notes.append(f"found {len(good)} of the {target} good-fit companies you asked for "
                     f"(researched {summary['companies']}); not padded with poor fits")
    else:
        notes.append(f"found {len(good)} good-fit companies after researching {summary['companies']}; stopped there")
    db.x("UPDATE runs SET summary=?, finished_at=?, updated_at=? WHERE id=?",
         (json.dumps(summary), now(), now(), run_id))
    return summary

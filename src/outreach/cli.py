"""outreach CLI."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from . import config
from .db import DB
from .discovery import discover, read_inputs
from .explain import explain as explain_text
from .llm import GeminiLLM, LLMError
from .pipeline import (Services, draft_company, poc_company, qualify_company, research,
                       run_pipeline)
from .util import registrable_domain

app = typer.Typer(add_completion=False, help="Sprinto cold-outreach pipeline")


def _svc(need_llm: bool = True) -> Services:
    db = DB(config.db_path())
    llm = None
    if need_llm:
        try:
            llm = GeminiLLM()
        except LLMError as e:
            typer.secho(f"{e}", fg="red")
            raise typer.Exit(2)
    return Services(db=db, llm=llm, cfg=config.icp())


def _dom(d: str) -> str:
    r = registrable_domain(d)
    if not r:
        typer.secho(f"not a valid domain: {d}", fg="red")
        raise typer.Exit(2)
    return r


@app.command()
def run(domains: Optional[Path] = typer.Option(None, "--domains", help="text file, one domain per line"),
        discover_: bool = typer.Option(False, "--discover", help="also search for more companies from the ICP"),
        limit: Optional[int] = typer.Option(None, "--limit"),
        min_companies: int = typer.Option(10, "--min-companies", help="target number of qualified companies")):
    """Whole pipeline up to the review queue. File lines may be domains OR company names.
    With no --domains, the system finds companies itself from the ICP (verified, never guessed)."""
    svc = _svc()
    doms, names = read_inputs(domains) if domains else ([], [])
    s = run_pipeline(svc, doms, discover_, limit, names, min_companies)
    for n in s.get("notes", []):
        typer.secho(f"  note: {n}", fg="yellow")
    typer.echo(f"{s['companies']} companies processed")
    for d, dec, score in s["qualified"]:
        typer.echo(f"  qualified: {d}  {dec}  score={score}")
    for d, st, did in s["drafts"]:
        typer.echo(f"  draft: {d}  poc={st}  draft_id={did}")
    for d, e in s["errors"].items():
        typer.secho(f"  error/dropped: {d}: {e}", fg="yellow")
    typer.echo("Next: `outreach review`")


@app.command("discover")
def discover_cmd():
    """Discovery only (RSS feeds + directory pages from config/icp.yaml)."""
    svc = _svc()
    found = discover(svc.db, svc.llm, svc.get_fetcher(), svc.cfg)
    for f in found:
        typer.echo(f"{f['domain']}\t{f['name']}\t{f['source_url']}")
    typer.echo(f"{len(found)} domain(s) with a sourced domain")


@app.command("research")
def research_cmd(domain: str):
    """Scrape + extract facts for one domain."""
    svc = _svc()
    typer.echo(research(svc, _dom(domain)))


@app.command()
def qualify(domain: str):
    """Go / No-Go from stored facts (no network, no LLM)."""
    svc = _svc(need_llm=False)
    q = qualify_company(svc, _dom(domain))
    typer.echo(q.model_dump_json(indent=2))


@app.command()
def poc(domain: str):
    """Find the decision-maker (Hunter, then manual file, then company page)."""
    svc = _svc(need_llm=False)
    typer.echo(poc_company(svc, _dom(domain)).model_dump_json(indent=2))


@app.command()
def draft(domain: str):
    """Pick pain points, draft the email, run the claim checker."""
    svc = _svc()
    did = draft_company(svc, _dom(domain))
    typer.echo(f"draft id: {did}" if did else "no draft: no named PoC (hand to a human)")


@app.command()
def review():
    """Human review gate."""
    from .review import interactive
    n = interactive(DB(config.db_path()))
    typer.echo(f"{n} draft(s) reviewed")


@app.command()
def send(draft_id: int):
    """Send the approved draft via Resend. TEST: to MY_EMAIL. LIVE: to the real recipient. Respects SEND_MODE."""
    from .send import SendBlocked, SendError, deliver
    try:
        r = deliver(DB(config.db_path()), draft_id)
    except (SendBlocked, SendError) as e:
        typer.secho(f"not sent: {e}", fg="red")
        raise typer.Exit(1)
    typer.echo(r)


@app.command()
def explain(domain: str):
    """Why this company, why this person, why this email - with sources."""
    typer.echo(explain_text(DB(config.db_path()), _dom(domain)))


@app.command("propose-rubric")
def propose_rubric_cmd():
    """Aggregate feedback and ask Gemini for rubric changes (written to config/rubric_proposals/)."""
    from .feedback import propose_rubric
    svc = _svc()
    try:
        p = propose_rubric(svc.db, svc.llm)
    except ValueError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.echo(f"proposal written: {p}\nReview it, then: outreach approve-rubric {p}")


@app.command("approve-rubric")
def approve_rubric(proposal: Path):
    """Approve each proposed change one by one; creates rubric_vN+1.yaml."""
    import yaml
    from .feedback import approve_proposal
    prop = yaml.safe_load(proposal.read_text(encoding="utf-8"))
    ok = [i for i, c in enumerate(prop["changes"])
          if typer.confirm(f"Apply change {i}: {c['description']}  ->  "
                           f"{c.get('new_guidance_line') or (c.get('weight_key'), c.get('new_weight'))}")]
    if not ok:
        typer.echo("nothing approved")
        raise typer.Exit(0)
    typer.echo(f"created {approve_proposal(DB(config.db_path()), proposal, ok)}")


@app.command()
def outcome(draft_id: int, outcome: str = typer.Argument(..., help="bounced | replied | positive_reply")):
    """Log a manual outcome for a sent draft."""
    from .feedback import record_outcome
    try:
        record_outcome(DB(config.db_path()), draft_id, outcome)
    except ValueError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    typer.echo("logged")


@app.command()
def drafts():
    """List drafts and their status."""
    for d in DB(config.db_path()).q("SELECT id,domain,status,subject FROM drafts ORDER BY id"):
        typer.echo(f"{d['id']:>3}  {d['status']:<20} {d['domain']:<28} {d['subject']}")


if __name__ == "__main__":
    app()

# Sprinto cold-outreach pipeline

Domains (or ICP discovery) -> scrape -> facts -> Go/No-Go -> decision-maker -> Gemini draft -> claim checker -> human review -> Gmail.
Every stage reads and writes SQLite (`data/outreach.db`), so any stage can be re-run alone.

## Rule that overrides everything: never hallucinate
- Every fact is stored as `{id, field, value, source_url, snippet, fetched_at}`. Missing = absent (null), never inferred.
- Gemini extraction (one call per company) may only copy text. Code checks each snippet is a substring of the cited page; if not, the field is discarded.
- Gemini drafting sees only verified facts (with ids), `config/sprinto_facts.yaml`, the pain-point library and the rubric. Never raw pages.
- `checker.py` blocks a draft with an uncited hook/pain, unknown fact ids, funding/hiring/cert/number/name claims not in the cited snippet, Sprinto claims outside `sprinto_facts.yaml`, a wrong recipient name, a competitor name, or any signal listed in `unknowns`. One regeneration, then `blocked`.
- Pattern-guessed emails are `unverified` and are never sent.

## Setup (Windows, PowerShell)
```powershell
cd outreach
python -m venv .venv ; .\.venv\Scripts\python -m pip install -e .
copy .env.example .env      # then fill it in
```
`.env`: `GEMINI_API_KEY`, `GEMINI_MODEL`, `HUNTER_API_KEY`, `MY_EMAIL`, `SEND_MODE` (TEST default), `GMAIL_CREDENTIALS_FILE`.
`GEMINI_MODEL` is read from the env, never hardcoded. `.env.example` suggests `gemini-3.6-flash` (the latest "Stable" Flash on Google's models page on 2026-11-04). Verify before use.

## Things only you can provide (placeholders, nothing invented)
- `config/sprinto_facts.yaml`: your real allowed claims and signature (currently one placeholder claim, S1).
- `config/icp.yaml` -> `discovery.rss_feeds` / `directory_pages`: empty until you add URLs.
- `config/manual_pocs.yaml`: Apollo lookups (`domain -> name/title/email/source`).
- `config/pain_points.yaml`: seeded with plausible entries; edit freely.
- Gmail OAuth `credentials.json` (scope `gmail.compose`); first `send` opens a browser consent once.

## Commands
```
outreach run [--domains file] [--discover] [--limit N] [--min-companies 10]   # up to the review queue
                                 # file lines = domains OR company names; EMPTY input = system finds companies from the ICP
outreach discover | research <d> | qualify <d> | poc <d> | draft <d>
outreach review                  # approve / edit / discard + good|bad + reason tag
outreach send <draft_id>         # TEST: sends to MY_EMAIL with [TEST] + "Intended recipient" banner
                                 # LIVE: creates a Gmail DRAFT only (never messages.send)
outreach explain <domain>        # why company, why person, why email, with facts + source URLs
outreach propose-rubric          # Gemini proposes rule changes -> config/rubric_proposals/
outreach approve-rubric <file>   # approve each change -> rubric_vN+1.yaml (hard gates never touched)
outreach outcome <id> bounced|replied|positive_reply
outreach drafts
```
Demo path: `run` -> `review` (approve) -> `send` -> email lands in your inbox -> `explain <domain>`.

## Two input modes
1. **You supply companies** (`--domains`, domains or plain names): names are resolved to a domain by search and verified.
2. **You supply nothing**: `config/icp.yaml` drives it. Gemini + Google Search grounding only *proposes* leads (queries are built from the ICP, or set `discovery.search_queries`); the lead is kept only if our own fetch of its homepage names the company and the domain resolves. Survivors go through the normal qualification. We over-fetch 2x `--min-companies` because many fail the ICP; if fewer than the target qualify, the run says so instead of padding with unverified names.
Google Search grounding is a paid-tier feature beyond a small free quota. Check current Gemini pricing before large runs.

## Design decisions worth knowing
- **Qualifier is code** (`qualify.py`): unknown B2B SaaS = `NO_GO` (or `INSUFFICIENT_DATA` if the fetch cap/unreachable site caused it); fully certified (SOC 2 Type II + ISO 27001) **and** on a listed competitor = drop; confirmed recent negative news = veto. Missing certification evidence = flag (`cert_status` unknown), not a drop, so most companies are `GO_FLAGGED`. Fully certified but not on a competitor tool gets a -2 weaker-fit penalty. Only score >= `send_threshold` continues.
- **Research order (mentor's change)**: Google News + RSS run FIRST, then our own scraper. The news step checks for a recent breach, layoffs, lawsuit or shutdown (a confirmed one vetoes the company and nothing is fetched from its site) and makes one attempt to fill a missing funding round. Before scraping we only know the given name and the domain, so headlines are matched against both (e.g. `northwind-labs` also matches "Northwind Labs"). The scraper, deterministic facts and the single Gemini extraction then run as before.
- **30-second scrape budget** (`limits.scrape_time_budget_seconds`, 0 = off): a wall-clock deadline per company covering only the scraping phase (not the Gemini extraction or the news step). No new fetch starts unless it can finish in time, and each request's timeout is clamped to what is left. Pages are fetched most-valuable first (security, pricing, careers, job board, about, trust, customers, then contact/product/privacy), so a cut only loses the least valuable pages. If the clock stops us before B2B SaaS is verified the company is `insufficient_data` (never guessed); cached pages are free, so a re-run resumes where it stopped. Related tuning: per-request timeout 15s -> 8s, retries 3 -> 2, per-host delay 1.5s -> 1.0s (still within a polite 1-2s); robots.txt and the 8-page cap are unchanged.
- **Agent loop**: the scraper fetches the homepage, then pages in priority order, skipping pages whose question is already answered (e.g. trust page once a cert fact exists), within `max_fetches_per_company`. At the cap with B2B unknown -> `insufficient_data`. Cached pages are free on re-runs.
- **Email status**: `verified` (Hunter `valid`, or your manual Apollo entry), `sourced` (Hunter `provenance: found` with a source page), `unverified` (everything else). Only `unverified` blocks sending. A named person without an email -> draft flagged `needs_human_email`.
- **Hunter**: free plan caps `limit + offset` at 10, so we request 10 senior/executive personal emails; responses cached forever per domain; monthly counter stops at 25.
- Scraper honors robots.txt, uses an honest User-Agent, waits 1.5 s per host, never touches LinkedIn, and uses Google News RSS (not Google HTML).

## Tests
`.\.venv\Scripts\python -m pytest` : 35 tests, fixtures only (saved HTML/RSS, fake Gemini, fake Gmail); no network or paid APIs. Covers the nine anti-hallucination cases, qualification gates, review validation, rubric-loop boundaries and a full pipeline run to a `[TEST]` email.

## Not verified / next
- Not run against live Gemini, Hunter or Gmail yet (needs your keys). The Gemini structured-output call (`llm.py`) and Gmail OAuth (`send.py`) are written to the official SDK shapes but untested live.
- Hunter response shape was checked against Hunter's docs (2026-11-04).
- Streamlit review page (CLI review only).
- Edited drafts are not re-run through the claim checker; the human is accountable for edits.
- No command yet to attach a verified email to a `needs_human_email` PoC (add it to `config/manual_pocs.yaml` and re-run `poc`).
- Pain-point pick is a Gemini call; selection is validated against the library but not scored in code.

## The UI (local web app)
```powershell
.\.venv\Scripts\python scripts\serve.py            # real data -> http://localhost:8000
.\.venv\Scripts\python scripts\seed_demo.py        # once: fictional demo data (no API calls)
.\.venv\Scripts\python scripts\serve.py --demo     # UI on the demo data (shows a DEMO banner)
```
Minimal + glassmorphism: a split hero (white space and one gradient sphere on the left, floating frosted-glass data cards on the right, filled from live API data) over a blue/violet/pink gradient with soft blurred blobs. Palette tokens live at the top of `web/styles.css` (`--glass-blue/violet/pink/teal`), so re-colouring (e.g. to Sprinto's plum/orange) is a one-block change. Accessibility: two glass tiers. True 15% glass with white text is used only for chrome and large numerals; every reading surface (company cards, email drafts, trace drawer, forms) uses a denser frosted fill with dark text. `prefers-reduced-transparency` and `prefers-reduced-motion` are honoured, and focus rings are visible. GSAP drives the scroll progress bar, hero entrance, panel/card staggers, the review card swap, the trace drawer and count-ups. Sections: Run (start/monitor, live log), Companies (score, decision, unknowns, trace), Review (card stack: approve/edit/discard/skip + label + reason tag; approved drafts go to a Send tray), Numbers.
It runs locally on purpose: keys, SQLite and the Gmail token stay on this machine, so it is not deployed to Vercel. The Send button obeys `SEND_MODE` (TEST = your own inbox; LIVE = Gmail draft only).

## Sprinto claims are chosen per prospect
`config/sprinto_facts.yaml` has a `use_when` hint per claim. The drafter picks the one claim that fits the prospect's verified facts (e.g. trust-center claim for a company with enterprise signals); the checker still rejects any Sprinto statement outside the list.

## Known limit: bot-protected sites
Some prospect sites (e.g. sprinto.com behind Cloudflare) answer our honest scraper with 403. We do not disguise the scraper; those companies are marked unreachable and skipped, never guessed.

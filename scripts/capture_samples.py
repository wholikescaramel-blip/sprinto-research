"""Capture REAL results into seed/sample_results.json for reviewers.
   python scripts/capture_samples.py [--want 4]

1. drafts any good-fit company in the real database that has no live draft yet,
2. runs discovery (stops at the target) if there are fewer good fits than --want,
3. snapshots every good fit with a draft (contact emails masked).
Nothing is invented: it only exports what the pipeline actually produced."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from outreach import config, samples  # noqa: E402
from outreach.api import ACTIVE_DRAFT, _draft_for  # noqa: E402
from outreach.db import DB  # noqa: E402
from outreach.llm import GeminiLLM  # noqa: E402
from outreach.pipeline import Services, run_pipeline  # noqa: E402

want = int(sys.argv[sys.argv.index("--want") + 1]) if "--want" in sys.argv else 4
t0 = time.time()
db = DB(config.db_path())
say = lambda m: print(f"[{time.time() - t0:5.0f}s] {m}", flush=True)


def good_fits():
    return [r["domain"] for r in db.q("SELECT domain FROM qualifications WHERE passes_threshold=1 ORDER BY score DESC")
            if db.one("SELECT origin FROM companies WHERE domain=?", (r["domain"],))["origin"] != "sample"]


# 0) re-judge what we already hold with the current rules (offline: no network, no Gemini)
from outreach.pipeline import qualify_company  # noqa: E402
_svc = Services(db=db, llm=None, cfg=config.icp())
for r in db.q("SELECT domain FROM companies WHERE origin <> 'sample'"):
    q = qualify_company(_svc, r["domain"])
    say(f"re-judged {r['domain']}: {q.decision} {q.score}" + ("" if q.passes_threshold else "  (not a good fit)"))

# 1) discovery first, if we are short of good fits
have = good_fits()
say(f"{len(have)} good fits already: {have}")
if len(have) < want:
    svc = Services(db=db, llm=GeminiLLM(), cfg=config.icp())
    s = run_pipeline(svc, [], min_companies=want - len(have), progress=lambda m: say(m.split("|", 1)[-1]))
    for n in s["notes"]:
        say("note: " + n)

# 2) a draft for every good fit that has none
for d in good_fits():
    drafts = [x for x in db.drafts_for(d) if x["status"] in ACTIVE_DRAFT]
    if drafts:
        continue
    say(f"drafting {d}…")
    try:
        say(f"   -> {_draft_for(db, d)}")
    except Exception as e:
        say(f"   draft failed: {str(e)[:160]}")

# 3) snapshot the good fits that now have a live draft
ready = [d for d in good_fits() if any(x["status"] in samples.LIVE_DRAFTS for x in db.drafts_for(d))]
out = samples.export_samples(db, ready, note="Real pipeline output (news -> scraper -> extraction -> Go/No-Go -> contact -> draft).")
say(f"exported {len(out['companies'])} sample companies: {[c['domain'] for c in out['companies']]}")

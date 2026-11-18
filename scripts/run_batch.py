"""Run the whole pipeline on a list of domains/names from the command line, printing progress.
   python scripts/run_batch.py clay.com linear.app "Rocketlane"      (uses the real local database)"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from outreach import config  # noqa: E402
from outreach.db import DB  # noqa: E402
from outreach.discovery import parse_inputs  # noqa: E402
from outreach.llm import GeminiLLM  # noqa: E402
from outreach.pipeline import Services, run_pipeline  # noqa: E402

doms, names = parse_inputs("\n".join(sys.argv[1:]))
t0 = time.time()
svc = Services(db=DB(config.db_path()), llm=GeminiLLM(), cfg=config.icp())


def progress(m: str):
    print(f"[{time.time() - t0:5.0f}s] {m.split('|', 1)[1] if '|' in m else m}", flush=True)


s = run_pipeline(svc, doms, False, None, names, min_companies=3, progress=progress)
print("\nQUALIFIED:", s["qualified"])
print("DRAFTS:", s["drafts"])
print("ERRORS:", s["errors"])
print("NOTES:", s["notes"], flush=True)

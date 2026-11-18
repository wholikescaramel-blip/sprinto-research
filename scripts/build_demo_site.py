"""Builds the static, read-only demo into demo/ (what Vercel serves). No keys, no database, no Python at runtime.

  python scripts/build_demo_site.py

It reseeds the FICTIONAL demo database, snapshots every API response the UI needs into demo/data/*.json, and
copies the UI with window.DEMO_STATIC=true so app.js reads those snapshots instead of calling the backend."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
subprocess.run([sys.executable, str(root / "scripts" / "seed_demo.py")], check=True)
os.environ["OUTREACH_DB"] = str(root / "data" / "demo.db")

from fastapi.testclient import TestClient  # noqa: E402

from outreach import api  # noqa: E402

out = root / "demo"
(out / "data" / "company").mkdir(parents=True, exist_ok=True)
(out / "static").mkdir(parents=True, exist_ok=True)
client = TestClient(api.app)


def dump(url: str, dest: Path, patch=None):
    r = client.get(url)
    r.raise_for_status()
    data = r.json()
    if patch:
        data = patch(data)
    dest.write_text(json.dumps(data, indent=1), encoding="utf-8")


def neutral_status(s: dict) -> dict:
    # nothing about this machine's keys, mail address or quotas goes public
    s.update(readonly=True, demo=True, my_email_set=False, gemini_ready=False, hunter_ready=False,
             gmail_ready=False, hunter_used=0, run={"running": False, "stage": None, "found": 0,
                                                    "started": None, "finished": None})
    return s


dump("/api/status", out / "data" / "status.json", neutral_status)
dump("/api/companies", out / "data" / "companies.json")
dump("/api/review-queue", out / "data" / "review-queue.json")
for c in client.get("/api/companies").json():
    dump(f"/api/company/{c['domain']}", out / "data" / "company" / f"{c['domain']}.json")

for name in ("styles.css", "app.js"):
    shutil.copyfile(root / "web" / name, out / "static" / name)
html = (root / "web" / "index.html").read_text(encoding="utf-8")
html = html.replace('<script src="/static/app.js"></script>',
                    '<script>window.DEMO_STATIC = true;</script>\n<script src="/static/app.js"></script>')
assert "DEMO_STATIC" in html
(out / "index.html").write_text(html, encoding="utf-8")
print("demo site written to", out, "-", len(list((out / "data").rglob("*.json"))), "data files")

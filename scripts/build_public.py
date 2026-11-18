"""Builds public/ (the static UI Vercel serves for the LIVE app) from web/.  python scripts/build_public.py
The UI talks to /api/* (the Python function) and learns it is hosted from /api/status, so no flag is injected."""
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / "public"
(out / "static").mkdir(parents=True, exist_ok=True)
for name in ("styles.css", "app.js"):
    shutil.copyfile(root / "web" / name, out / "static" / name)
shutil.copyfile(root / "web" / "index.html", out / "index.html")
print("public/ written:", sorted(p.name for p in out.rglob("*") if p.is_file()))

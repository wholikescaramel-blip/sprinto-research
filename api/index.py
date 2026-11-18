"""Vercel entrypoint: exposes the FastAPI app as a Python function. All /api/* requests are rewritten here
(see vercel.json). The UI itself is static (public/) and served by Vercel's CDN."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("OUTREACH_HOME", str(ROOT))   # config/ ships inside the function bundle
sys.path.insert(0, str(ROOT / "src"))

from outreach.api import app  # noqa: E402,F401  (Vercel looks for a top-level `app`)

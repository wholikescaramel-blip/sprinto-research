"""Start the UI.  python scripts/serve.py          -> real data (data/outreach.db)
                 python scripts/serve.py --demo   -> fictional demo data (data/demo.db, run seed_demo.py first)"""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
if "--demo" in sys.argv:
    os.environ["OUTREACH_DB"] = str(root / "data" / "demo.db")
if "--public" in sys.argv:  # behave like the hosted app: masked emails, TEST-only sends, daily caps, step API
    os.environ["PUBLIC_MODE"] = "1"
port = int(os.environ.get("PORT", "8000"))

import uvicorn  # noqa: E402

uvicorn.run("outreach.api:app", host="127.0.0.1", port=port, log_level="warning")

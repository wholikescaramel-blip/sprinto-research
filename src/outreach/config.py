"""Config + env loading. ICP is data, not prompts."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("OUTREACH_HOME") or Path(__file__).resolve().parents[2])
CONFIG_DIR = ROOT / "config"
load_dotenv(ROOT / ".env")

SOFT_WEIGHT_KEYS = {"compliance_trigger", "stage_fit", "eu_regulated"}


def _load(name: str) -> dict:
    p = CONFIG_DIR / name
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def send_mode() -> str:
    m = (env("SEND_MODE", "TEST") or "TEST").upper()
    return m if m in ("TEST", "LIVE") else "TEST"


def db_path():
    """Postgres URL when DATABASE_URL is set (the hosted app on Neon), else the local SQLite file."""
    url = env("DATABASE_URL")
    if url:
        return url
    return Path(env("OUTREACH_DB") or ROOT / "data" / "outreach.db")


def icp() -> dict:
    return _load("icp.yaml")


def sprinto_facts() -> dict:
    return _load("sprinto_facts.yaml")


def pain_points() -> dict:
    return _load("pain_points.yaml")


def manual_pocs() -> dict:
    return _load("manual_pocs.yaml")


def latest_rubric_version() -> int:
    vs = []
    for p in CONFIG_DIR.glob("rubric_v*.yaml"):
        try:
            vs.append(int(p.stem.split("_v")[1]))
        except ValueError:
            pass
    return max(vs) if vs else 1


def rubric(version: int | None = None) -> dict:
    v = version or latest_rubric_version()
    r = _load(f"rubric_v{v}.yaml")
    r.setdefault("version", v)
    return r


def weights(rubric_cfg: dict | None = None) -> dict:
    """ICP weights, overlaid by rubric soft_weights (soft signals only)."""
    w = dict((icp().get("weights") or {}))
    r = rubric_cfg if rubric_cfg is not None else rubric()
    for k, v in (r.get("soft_weights") or {}).items():
        if k in SOFT_WEIGHT_KEYS:
            w[k] = v
    return w

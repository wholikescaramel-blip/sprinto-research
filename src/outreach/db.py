"""SQLite storage. Every stage reads/writes here so any stage can be re-run alone."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  domain TEXT PRIMARY KEY, name TEXT, source_url TEXT, origin TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS stage_status(
  domain TEXT, stage TEXT, status TEXT, error TEXT, updated_at TEXT,
  PRIMARY KEY(domain, stage));
CREATE TABLE IF NOT EXISTS pages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, url TEXT UNIQUE,
  status_code INTEGER, content_hash TEXT, text TEXT, links TEXT,
  raw_emails TEXT, fetched_at TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT, domain TEXT, field TEXT, value TEXT, source_url TEXT, snippet TEXT,
  page_id INTEGER, fetched_at TEXT, created_at TEXT, updated_at TEXT,
  PRIMARY KEY(domain, id),
  FOREIGN KEY(domain) REFERENCES companies(domain),
  FOREIGN KEY(page_id) REFERENCES pages(id));
CREATE TABLE IF NOT EXISTS qualifications(
  domain TEXT PRIMARY KEY, decision TEXT, score REAL, confidence TEXT,
  reasons TEXT, unknowns TEXT, passes_threshold INTEGER, rubric_version INTEGER,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS pocs(
  domain TEXT PRIMARY KEY, data TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS hunter_cache(
  domain TEXT PRIMARY KEY, response TEXT, fetched_at TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS counters(
  name TEXT PRIMARY KEY, value INTEGER, period TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, subject TEXT, body TEXT,
  sentences TEXT, pain_points TEXT, greeting_name TEXT, rubric_version INTEGER,
  status TEXT, check_result TEXT, poc TEXT, gmail_id TEXT, sent_mode TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, action TEXT,
  label TEXT, reason_tag TEXT, note TEXT, original_text TEXT, final_text TEXT,
  diff TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER, outcome TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS rubric_versions(
  version INTEGER PRIMARY KEY, path TEXT, source_proposal TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, args TEXT, summary TEXT,
  started_at TEXT, finished_at TEXT, created_at TEXT, updated_at TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fact_id(domain: str, field: str, value: str, source_url: str) -> str:
    h = hashlib.sha1(f"{domain}|{field}|{value}|{source_url}".encode()).hexdigest()[:7]
    return f"F{h}"


class DB:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # -- generic helpers
    def q(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable[Any] = ()):
        r = self.q(sql, args)
        return r[0] if r else None

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = self.conn.execute(sql, tuple(args))
        self.conn.commit()
        return cur.lastrowid

    # -- companies / stages
    def upsert_company(self, domain: str, name: str | None = None,
                       source_url: str | None = None, origin: str = "manual"):
        t = now()
        self.x("""INSERT INTO companies(domain,name,source_url,origin,created_at,updated_at)
                  VALUES(?,?,?,?,?,?)
                  ON CONFLICT(domain) DO UPDATE SET
                    name=COALESCE(excluded.name,name),
                    source_url=COALESCE(excluded.source_url,source_url),
                    updated_at=excluded.updated_at""",
               (domain, name, source_url, origin, t, t))

    def set_stage(self, domain: str, stage: str, status: str, error: str | None = None):
        self.x("""INSERT INTO stage_status(domain,stage,status,error,updated_at) VALUES(?,?,?,?,?)
                  ON CONFLICT(domain,stage) DO UPDATE SET status=excluded.status,
                  error=excluded.error, updated_at=excluded.updated_at""",
               (domain, stage, status, error, now()))

    def stage(self, domain: str, stage: str) -> str | None:
        r = self.one("SELECT status FROM stage_status WHERE domain=? AND stage=?", (domain, stage))
        return r["status"] if r else None

    # -- pages
    def get_page(self, url: str):
        return self.one("SELECT * FROM pages WHERE url=?", (url,))

    def save_page(self, domain: str, url: str, status_code: int, text: str,
                  links: list[str], emails: list[str]) -> int:
        t = now()
        h = hashlib.sha256(text.encode()).hexdigest()
        self.x("""INSERT INTO pages(domain,url,status_code,content_hash,text,links,raw_emails,
                  fetched_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(url) DO UPDATE SET status_code=excluded.status_code,
                  content_hash=excluded.content_hash, text=excluded.text, links=excluded.links,
                  raw_emails=excluded.raw_emails, fetched_at=excluded.fetched_at,
                  updated_at=excluded.updated_at""",
               (domain, url, status_code, h, text, json.dumps(links), json.dumps(emails), t, t, t))
        return self.get_page(url)["id"]

    def pages_for(self, domain: str):
        return self.q("SELECT * FROM pages WHERE domain=? AND status_code BETWEEN 200 AND 299 "
                      "ORDER BY id", (domain,))

    # -- facts
    def add_fact(self, domain: str, field: str, value: str, source_url: str,
                 snippet: str, page_id: int | None = None) -> str:
        fid = fact_id(domain, field, value, source_url)
        t = now()
        self.x("""INSERT OR IGNORE INTO facts(id,domain,field,value,source_url,snippet,page_id,
                  fetched_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (fid, domain, field, value, source_url, snippet, page_id, t, t, t))
        return fid

    def facts(self, domain: str, field: str | None = None):
        if field:
            return self.q("SELECT * FROM facts WHERE domain=? AND field=? ORDER BY id", (domain, field))
        return self.q("SELECT * FROM facts WHERE domain=? ORDER BY field,id", (domain,))

    def fact_by_id(self, domain: str, fid: str):
        return self.one("SELECT * FROM facts WHERE domain=? AND id=?", (domain, fid))

    def clear_facts(self, domain: str):
        self.x("DELETE FROM facts WHERE domain=?", (domain,))

    # -- qualification
    def save_qual(self, q) -> None:
        t = now()
        self.x("""INSERT INTO qualifications(domain,decision,score,confidence,reasons,unknowns,
                  passes_threshold,rubric_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(domain) DO UPDATE SET decision=excluded.decision,score=excluded.score,
                  confidence=excluded.confidence,reasons=excluded.reasons,unknowns=excluded.unknowns,
                  passes_threshold=excluded.passes_threshold,rubric_version=excluded.rubric_version,
                  updated_at=excluded.updated_at""",
               (q.domain, q.decision, q.score, q.confidence,
                json.dumps([r.model_dump() for r in q.reasons]), json.dumps(q.unknowns),
                int(q.passes_threshold), q.rubric_version, t, t))

    def get_qual(self, domain: str):
        from .models import QualResult, Reason
        r = self.one("SELECT * FROM qualifications WHERE domain=?", (domain,))
        if not r:
            return None
        return QualResult(domain=r["domain"], decision=r["decision"], score=r["score"],
                          confidence=r["confidence"],
                          reasons=[Reason(**x) for x in json.loads(r["reasons"])],
                          unknowns=json.loads(r["unknowns"]),
                          passes_threshold=bool(r["passes_threshold"]),
                          rubric_version=r["rubric_version"])

    # -- poc
    def save_poc(self, poc) -> None:
        t = now()
        self.x("""INSERT INTO pocs(domain,data,created_at,updated_at) VALUES(?,?,?,?)
                  ON CONFLICT(domain) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at""",
               (poc.domain, poc.model_dump_json(), t, t))

    def get_poc(self, domain: str):
        from .models import Poc
        r = self.one("SELECT data FROM pocs WHERE domain=?", (domain,))
        return Poc.model_validate_json(r["data"]) if r else None

    # -- hunter quota
    def hunter_cached(self, domain: str):
        r = self.one("SELECT response FROM hunter_cache WHERE domain=?", (domain,))
        return json.loads(r["response"]) if r else None

    def hunter_store(self, domain: str, response: dict):
        t = now()
        self.x("""INSERT OR REPLACE INTO hunter_cache(domain,response,fetched_at,created_at,updated_at)
                  VALUES(?,?,?,?,?)""", (domain, json.dumps(response), t, t, t))

    def bump_counter(self, name: str, period: str) -> int:
        t = now()
        r = self.one("SELECT value, period FROM counters WHERE name=?", (name,))
        v = (r["value"] + 1) if (r and r["period"] == period) else 1
        self.x("""INSERT OR REPLACE INTO counters(name,value,period,created_at,updated_at)
                  VALUES(?,?,?,?,?)""", (name, v, period, t, t))
        return v

    def counter(self, name: str, period: str) -> int:
        r = self.one("SELECT value, period FROM counters WHERE name=?", (name,))
        return r["value"] if (r and r["period"] == period) else 0

    # -- drafts
    def save_draft(self, **k) -> int:
        t = now()
        cols = ["domain", "subject", "body", "sentences", "pain_points", "greeting_name",
                "rubric_version", "status", "check_result", "poc"]
        vals = [k.get(c) for c in cols]
        return self.x(f"INSERT INTO drafts({','.join(cols)},created_at,updated_at) "
                      f"VALUES({','.join('?' * len(cols))},?,?)", (*vals, t, t))

    def get_draft(self, draft_id: int):
        return self.one("SELECT * FROM drafts WHERE id=?", (draft_id,))

    def drafts_for(self, domain: str):
        return self.q("SELECT * FROM drafts WHERE domain=? ORDER BY id", (domain,))

    def update_draft(self, draft_id: int, **fields):
        sets = ",".join(f"{c}=?" for c in fields)
        self.x(f"UPDATE drafts SET {sets}, updated_at=? WHERE id=?",
               (*fields.values(), now(), draft_id))

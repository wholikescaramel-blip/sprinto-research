"""The DB layer must behave identically on SQLite (local) and Postgres (hosted). Offline checks here;
the live Postgres round-trip runs when DATABASE_URL points at a real database (see test at the bottom)."""
from __future__ import annotations

import os

import pytest

from outreach.db import DB, PG_SCHEMA, SCHEMA, is_pg_url, pg_sql


def test_url_detection_and_placeholder_translation():
    assert is_pg_url("postgres://u:p@h/db") and is_pg_url("postgresql://u:p@h/db")
    assert not is_pg_url(":memory:") and not is_pg_url("data/outreach.db")
    assert pg_sql("SELECT * FROM t WHERE a=? AND b LIKE '%x%'") == "SELECT * FROM t WHERE a=%s AND b LIKE '%%x%%'"


def test_postgres_schema_is_the_same_tables_with_pg_types():
    assert "AUTOINCREMENT" not in PG_SCHEMA and "SERIAL PRIMARY KEY" in PG_SCHEMA
    assert "DOUBLE PRECISION" in PG_SCHEMA and " REAL," not in PG_SCHEMA
    tables = lambda s: sorted(x.split("(")[0].split()[-1] for x in s.split(";") if "CREATE TABLE" in x)
    assert tables(SCHEMA) == tables(PG_SCHEMA)


def test_counter_is_atomic_per_period_and_resets_on_a_new_one(db):
    assert [db.bump_counter("research", "2026-11-18") for _ in range(3)] == [1, 2, 3]
    assert db.counter("research", "2026-11-18") == 3
    assert db.bump_counter("research", "2026-11-19") == 1   # new day -> fresh count
    assert db.counter("research", "2026-11-18") == 0         # old period no longer counts
    assert db.bump_counter("draft", "2026-11-19") == 1       # counters are independent


def test_duplicate_fact_is_ignored_not_an_error(db):
    db.upsert_company("x.io")
    a = db.add_fact("x.io", "grc_job", "Head of GRC", "https://x.io/c", "Head of GRC")
    b = db.add_fact("x.io", "grc_job", "Head of GRC", "https://x.io/c", "Head of GRC")
    assert a == b and len(db.facts("x.io")) == 1


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to a throwaway Postgres")
def test_postgres_round_trip():
    d = DB(os.environ["TEST_DATABASE_URL"])
    dom = "pgtest.example"
    d.x("DELETE FROM facts WHERE domain=?", (dom,))
    d.upsert_company(dom, "PG Test")
    d.add_fact(dom, "grc_job", "Head of GRC", "https://pgtest.example/c", "Head of GRC")
    d.add_fact(dom, "grc_job", "Head of GRC", "https://pgtest.example/c", "Head of GRC")  # duplicate ignored
    assert len(d.facts(dom)) == 1
    did = d.save_draft(domain=dom, subject="s", body="b", sentences="[]", pain_points="[]", greeting_name="A",
                       rubric_version=1, status="pending_review", check_result="{}", poc="{}")
    assert isinstance(did, int) and d.get_draft(did)["status"] == "pending_review"
    d.update_draft(did, status="approved")
    assert d.get_draft(did)["status"] == "approved"
    n1 = d.bump_counter("pgtest", "p1")
    assert d.bump_counter("pgtest", "p1") == n1 + 1

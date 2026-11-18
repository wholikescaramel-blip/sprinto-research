"""What went wrong in the first real run: Gemini quota (429) and overload (503) were swallowed and the run just
said "0 companies". Now: fall back, verify anyway, and always say why."""
from __future__ import annotations

import pytest

from conftest import make_fetcher
from outreach import config, llm as llm_mod
from outreach.discovery import icp_search, parse_inputs, resolve_names
from outreach.llm import GeminiLLM, LLMError, QuotaError

OK = lambda d: "1.2.3.4"


# ---- Gemini model chain ---------------------------------------------------------------------------------
@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    llm_mod._COOLDOWN.clear()
    g = GeminiLLM.__new__(GeminiLLM)        # no real client needed to test the retry/fallback logic
    g.models = ["m1", "m2", "m3"]
    return g


def test_overloaded_model_falls_through_to_the_next_model(chain):
    seen = []

    def fn(model):
        seen.append(model)
        if model == "m1":
            raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand")
        return f"answer from {model}"
    assert chain._call(fn, "call") == "answer from m2"
    assert seen == ["m1", "m2"]             # no wasted retry on the busy model: straight to the next one


def test_a_model_that_just_failed_rests_so_later_calls_skip_it(chain):
    seen = []

    def fn(model):
        seen.append(model)
        if model == "m1":
            raise RuntimeError("504 DEADLINE_EXCEEDED")
        return model
    chain._call(fn, "call")
    seen.clear()
    assert chain._call(fn, "call") == "m2" and seen == ["m2"]   # m1 is cooling down, not retried


def test_out_of_quota_model_is_skipped_without_retrying(chain):
    seen = []

    def fn(model):
        seen.append(model)
        if model == "m1":
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"
    assert chain._call(fn, "call") == "ok" and seen == ["m1", "m2"]


DAILY = ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric generate_content_free_tier_requests, limit: 20, "
         "description: Generate content requests per day limit exceeded on Free Tier. Please retry in 16h20m59.59s.")
MINUTE = "429 RESOURCE_EXHAUSTED. Generate content requests per minute limit exceeded. Please retry in 23.4s."


def test_retry_hint_parsing():
    from outreach.llm import _is_daily, _retry_seconds
    assert _retry_seconds(RuntimeError(DAILY)) == 16 * 3600 + 20 * 60 + 59.59
    assert _retry_seconds(RuntimeError(MINUTE)) == 23.4 and _retry_seconds(RuntimeError("429 no hint")) is None
    assert _is_daily(RuntimeError(DAILY)) and not _is_daily(RuntimeError(MINUTE))


def test_a_model_with_no_daily_quota_left_is_skipped_for_hours_not_retried(chain):
    seen = []

    def fn(model):
        seen.append(model)
        if model == "m1":
            raise RuntimeError(DAILY)
        return model
    assert chain._call(fn, "call") == "m2"
    seen.clear()
    assert chain._call(fn, "call") == "m2" and seen == ["m2"]            # m1 stays out for the day (capped at 6h)
    assert llm_mod._COOLDOWN["m1"] - llm_mod.time.time() <= llm_mod.MAX_DAILY_COOLDOWN + 1


def test_when_every_model_is_rate_limited_for_a_moment_it_waits_once_and_succeeds(chain, monkeypatch):
    slept, calls = [], {"n": 0}
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: slept.append(s))

    def fn(model):
        calls["n"] += 1
        if calls["n"] <= 3:                                              # first pass: all three models limited
            raise RuntimeError(MINUTE)
        return "ok"
    assert chain._call(fn, "call") == "ok" and any(abs(s - 24.4) < 0.01 for s in slept)


def test_a_long_per_minute_wait_is_not_waited_out(chain, monkeypatch):
    slept = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: slept.append(s))

    def fn(model):
        raise RuntimeError("429 RESOURCE_EXHAUSTED per minute limit. Please retry in 300s.")
    with pytest.raises(QuotaError):
        chain._call(fn, "call")
    assert slept == []                                                   # 300s > MAX_MINUTE_WAIT: fail fast, say why


def test_all_models_out_of_quota_raises_a_quota_error(chain):
    def fn(model):
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
    with pytest.raises(QuotaError, match="m1, m2, m3"):
        chain._call(fn, "grounded search")


def test_when_every_model_is_resting_they_are_all_tried_anyway(chain):
    for m in chain.models:
        llm_mod._COOLDOWN[m] = 10**12
    assert chain._call(lambda model: f"ok {model}", "call") == "ok m1"


def test_other_failures_raise_a_plain_llm_error_with_the_reason(chain):
    def fn(model):
        raise ValueError("bad json from model")
    with pytest.raises(LLMError, match="bad json") as e:
        chain._call(fn, "call")
    assert not isinstance(e.value, QuotaError)


# ---- discovery never fails silently ---------------------------------------------------------------------
class QuotaThenKnowledge:
    """Google Search is out of quota; plain Gemini still answers."""

    def grounded_search(self, prompt):
        raise QuotaError("429 RESOURCE_EXHAUSTED")

    def generate_text(self, prompt):
        return "Acmeflow | acmeflow.io\nGhostco | ghostco.io\nWrongname | acmeflow.io\n"


class AllDown:
    def grounded_search(self, prompt):
        raise QuotaError("429 RESOURCE_EXHAUSTED")

    def generate_text(self, prompt):
        raise LLMError("503 UNAVAILABLE high demand")


def test_search_quota_falls_back_to_gemini_knowledge_but_every_lead_is_still_verified(db):
    notes: list[str] = []
    found = icp_search(db, QuotaThenKnowledge(), make_fetcher(db), config.icp(), target=5, max_queries=1,
                       resolver=OK, notes=notes)
    # Ghostco's page never names it -> dropped; 'Wrongname' repeats an already-verified domain -> skipped
    assert [f["domain"] for f in found] == ["acmeflow.io"]
    text = " ".join(notes)
    assert "quota exhausted" in text and "verified against its own homepage" in text
    assert "Checked 2 lead(s), verified 1" in text and "Dropped: 1 name not on its homepage" in text


def test_competitors_are_never_discovered_as_prospects(db):
    class WithCompetitors:
        def grounded_search(self, prompt):
            return "Vanta | vanta.com\nDrata | drata.com\nAcmeflow | acmeflow.io\n"
    notes: list[str] = []
    found = icp_search(db, WithCompetitors(), make_fetcher(db), config.icp(), 5, 1, OK, notes=notes)
    assert [f["domain"] for f in found] == ["acmeflow.io"]
    assert any("Skipped competitors: Drata, Vanta" in n for n in notes)


def test_when_everything_is_down_the_reason_is_reported_not_swallowed(db):
    notes: list[str] = []
    assert icp_search(db, AllDown(), make_fetcher(db), config.icp(), 5, 1, OK, notes=notes) == []
    assert any("Gemini is unavailable" in n and "overloaded" in n for n in notes)


def test_name_lookup_explains_why_a_name_did_not_resolve(db):
    class Unknown:
        def grounded_search(self, p):
            return "UNKNOWN"
    notes: list[str] = []
    assert resolve_names(db, Unknown(), make_fetcher(db), ["Totally Made Up Co"], OK, notes) == []
    assert any("Totally Made Up Co" in n and "Paste its domain" in n for n in notes)


def test_comma_and_semicolon_separated_entries_are_split():
    assert parse_inputs("acmeflow, rocketlane\nacme.io; globex.com  # comment") == (
        ["acme.io", "globex.com"], ["acmeflow", "rocketlane"])

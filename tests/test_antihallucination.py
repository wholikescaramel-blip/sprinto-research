"""The nine anti-hallucination tests from section 8 of the build prompt."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeLLM, FakeResend, line_with, make_fetcher
from outreach import config, news
from outreach.checker import check_draft
from outreach.db import DB
from outreach.discovery import verify_discovered
from outreach.extract import deterministic_facts, llm_extract
from outreach.models import (DiscoveredCompany, DraftOut, DraftSentence, ExtractionOut, FieldOut,
                             Poc)
from outreach.pipeline import Services, qualify_company, research
from outreach.poc import find_poc, guess_email
from outreach.scraper import scrape_company
from outreach.send import SendBlocked, SendError, check_sendable, deliver

ICP = config.icp()


def _facts_row(fid, field, value, snippet, url="https://x.io/"):
    return {"id": fid, "field": field, "value": value, "snippet": snippet, "source_url": url}


def _poc(**k):
    d = dict(domain="x.io", status="ok", name="Priya Nair", title="Head of Security",
             email="priya@x.io", email_status="verified", source="hunter", role_type="security_head")
    d.update(k)
    return Poc(**d)


def _draft(sentences, greeting="Priya", subject="Quick question"):
    return DraftOut(subject=subject, greeting_name=greeting, sentences=sentences)


# 1 ------------------------------------------------------------------
def test_page_without_soc2_gives_null_not_none(db):
    db.upsert_company("plainwork.io")
    fetcher = make_fetcher(db, {"/": "nosoc2_home.html"})
    scrape_company(db, "plainwork.io", fetcher)
    deterministic_facts(db, "plainwork.io", ICP["competitors"])
    llm = FakeLLM({ExtractionOut: lambda p: ExtractionOut()})  # model finds nothing -> nulls
    llm_extract(db, "plainwork.io", llm)
    fields = {r["field"] for r in db.facts("plainwork.io")}
    assert not any(f.startswith("cert_") or "soc2" in f for f in fields)
    assert not any("not certified" in r["value"].lower() or r["value"].lower() == "none"
                   for r in db.facts("plainwork.io"))
    # qualification treats it as UNKNOWN (flag), never as "not certified"
    db.add_fact("plainwork.io", "is_b2b_saas", "true", "https://plainwork.io/", "Book a demo")
    q = qualify_company(Services(db=db, cfg=ICP), "plainwork.io")
    assert "cert_status" in q.unknowns


# 2 ------------------------------------------------------------------
def test_snippet_not_in_source_is_discarded(db):
    db.upsert_company("acmeflow.io")
    scrape_company(db, "acmeflow.io", make_fetcher(db))

    def handler(prompt):
        url, line = line_with(prompt, "Book a demo")
        return ExtractionOut(
            is_b2b_saas=FieldOut(value="true", source_url=url, snippet=line),                 # real
            funding_stage=FieldOut(value="Series B", source_url=url,
                                   snippet="Acmeflow raised a Series B led by Sequoia"))       # invented
    info = llm_extract(db, "acmeflow.io", FakeLLM({ExtractionOut: handler}))
    fields = {r["field"] for r in db.facts("acmeflow.io")}
    assert "is_b2b_saas" in fields
    assert "funding_stage" not in fields
    assert any("funding_stage" in d for d in info["discarded"])


# 3 ------------------------------------------------------------------
def test_checker_blocks_hook_without_fact_id():
    d = _draft([DraftSentence(text="Noticed your team is growing fast.", type="hook", fact_ids=[]),
                DraftSentence(text="Is audit prep slowing you down?", type="pain", fact_ids=["F1"]),
                DraftSentence(text="Worth a chat?", type="cta")])
    facts = {"F1": _facts_row("F1", "grc_job", "Head of GRC", "Head of GRC")}
    r = check_draft(d, _poc(), facts, {"S1": "x"}, [], ["Acmeflow"])
    assert not r.passed and any("no company fact id" in f for f in r.failures)


def test_checker_blocks_unknown_fact_id():
    d = _draft([DraftSentence(text="Saw the GRC opening.", type="hook", fact_ids=["F999"]),
                DraftSentence(text="Is audit prep slow?", type="pain", fact_ids=["F999"])])
    r = check_draft(d, _poc(), {}, {}, [], ["Acmeflow"])
    assert not r.passed and any("unknown fact id" in f for f in r.failures)


# 4 ------------------------------------------------------------------
def test_checker_blocks_funding_and_hiring_claims_not_in_snippet():
    facts = {"F1": _facts_row("F1", "product_description", "workflow software",
                              "Acmeflow is workflow software for operations teams.")}
    d = _draft([DraftSentence(text="Congrats on the Series B and the hiring push.", type="hook",
                              fact_ids=["F1"]),
                DraftSentence(text="Is audit prep slow?", type="pain", fact_ids=["F1"])])
    r = check_draft(d, _poc(), facts, {}, [], ["Acmeflow"])
    joined = " ".join(r.failures)
    assert not r.passed and "funding claim" in joined and "hiring claim" in joined


def test_checker_blocks_number_not_in_snippet_and_passes_supported_draft():
    facts = {"F1": _facts_row("F1", "headcount", "85 employees", "a team of 85 employees"),
             "F2": _facts_row("F2", "grc_job", "Head of GRC", "Head of GRC")}
    sp = {"S1": "Sprinto automates compliance certifications such as SOC 2, ISO 27001 and GDPR."}
    bad = _draft([DraftSentence(text="With 200 people you must be busy.", type="hook", fact_ids=["F1"]),
                  DraftSentence(text="Is audit prep slow?", type="pain", fact_ids=["F2"])])
    assert any("number '200'" in f for f in check_draft(bad, _poc(), facts, sp, [], ["Acmeflow"]).failures)
    good = _draft([DraftSentence(text="Saw you're hiring a Head of GRC.", type="hook", fact_ids=["F2"]),
                   DraftSentence(text="Is audit evidence collection eating your week?", type="pain",
                                 fact_ids=["F2"]),
                   DraftSentence(text="Sprinto automates compliance certifications such as SOC 2, "
                                      "ISO 27001 and GDPR.", type="pitch", fact_ids=["S1"]),
                   DraftSentence(text="Open to a quick chat?", type="cta")])
    r = check_draft(good, _poc(), facts, sp, [], ["Acmeflow"])
    assert r.passed, r.failures


def test_checker_blocks_invented_sprinto_claim_and_wrong_recipient():
    facts = {"F2": _facts_row("F2", "grc_job", "Head of GRC", "Head of GRC")}
    sp = {"S1": "Sprinto automates compliance certifications."}
    d = _draft([DraftSentence(text="Saw you're hiring a Head of GRC.", type="hook", fact_ids=["F2"]),
                DraftSentence(text="Is audit prep slow?", type="pain", fact_ids=["F2"]),
                DraftSentence(text="Sprinto is trusted by 500 customers.", type="pitch", fact_ids=["S1"])],
               greeting="Priyanka")
    r = check_draft(d, _poc(), facts, sp, [], ["Acmeflow"])
    joined = " ".join(r.failures)
    assert "Sprinto claim not in sprinto_facts" in joined and "does not match stored PoC" in joined


# 5 ------------------------------------------------------------------
def test_guessed_email_is_never_sent(db):
    # a named person on the company page + Hunter's pattern -> suggestion only, never `email`
    db.upsert_company("x.io")
    db.add_fact("x.io", "person", "Priya Nair | Head of Security", "https://x.io/security",
                "Priya Nair, Head of Security, leads our security program.")
    poc = find_poc(db, "x.io", api_key=None, icp_cfg=ICP, manual={})
    assert poc.status == "needs_human_email" and poc.email is None
    assert guess_email("Priya Nair", "x.io", "{first}.{last}") == "priya.nair@x.io"
    guessed = _poc(email="priya.nair@x.io", email_status="unverified", source="guess")
    draft = {"status": "approved", "check_result": '{"passed": true}'}
    with pytest.raises(SendBlocked, match="unverified"):
        check_sendable(draft, guessed)
    resend = FakeResend()
    did = db.save_draft(domain="x.io", subject="s", body="b", sentences="[]", pain_points="[]",
                        greeting_name="Priya", rubric_version=1, status="approved",
                        check_result='{"passed": true}', poc=guessed.model_dump_json())
    with pytest.raises(SendBlocked):
        deliver(db, did, client=resend.client, mode="TEST")
    assert resend.calls == []


# 6 ------------------------------------------------------------------
@pytest.fixture
def mail_env(monkeypatch):
    monkeypatch.setenv("MY_EMAIL", "me@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.delenv("PUBLIC_MODE", raising=False)


def _approved_draft(db):
    db.upsert_company("x.io")
    return db.save_draft(domain="x.io", subject="GRC hiring", body="Hi Priya,\n\nbody", sentences="[]",
                         pain_points="[]", greeting_name="Priya", rubric_version=1,
                         status="approved", check_result='{"passed": true}',
                         poc=_poc().model_dump_json())


def test_test_mode_sends_only_to_my_inbox_with_banner(db, mail_env):
    resend = FakeResend()
    r = deliver(db, _approved_draft(db), client=resend.client, mode="TEST")
    (call,) = resend.calls
    assert call["url"] == "https://api.resend.com/emails"
    assert call["json"]["to"] == ["me@example.com"] and call["json"]["subject"].startswith("[TEST] ")
    assert "Intended recipient: Priya Nair <priya@x.io>" in call["json"]["text"]
    assert call["headers"]["authorization"] == "Bearer re_test_key"
    assert r["status"] == "sent_test" and r["message_id"] == "re_123"


def test_live_mode_sends_the_approved_draft_to_the_real_recipient(db, mail_env):
    resend = FakeResend()
    did = _approved_draft(db)
    r = deliver(db, did, client=resend.client, mode="LIVE")
    (call,) = resend.calls
    assert call["json"]["to"] == ["priya@x.io"] and call["json"]["reply_to"] == ["me@example.com"]
    assert call["json"]["subject"] == "GRC hiring" and "[TEST]" not in call["json"]["text"]
    assert call["headers"]["idempotency-key"] == f"outreach-draft-{did}-LIVE"  # double click cannot double-send
    assert r["status"] == "sent_live" and db.get_draft(did)["status"] == "sent_live"


def test_public_hosted_app_is_forced_to_test_even_if_live_is_requested(db, mail_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    resend = FakeResend()
    r = deliver(db, _approved_draft(db), client=resend.client, mode="LIVE")
    assert resend.calls[0]["json"]["to"] == ["me@example.com"] and r["mode"] == "TEST"


def test_unapproved_or_failed_check_is_not_sent(db, mail_env):
    resend = FakeResend()
    did = _approved_draft(db)
    db.update_draft(did, check_result='{"passed": false, "failures": ["x"]}')
    with pytest.raises(SendBlocked, match="checker"):
        deliver(db, did, client=resend.client, mode="TEST")
    db.update_draft(did, check_result='{"passed": true}', status="pending_review")
    with pytest.raises(SendBlocked, match="approved"):
        deliver(db, did, client=resend.client, mode="LIVE")
    assert resend.calls == []


def test_resend_error_is_reported_and_the_draft_stays_unsent(db, mail_env):
    resend = FakeResend(status=403, body={"message": "The example.com domain is not verified"})
    did = _approved_draft(db)
    with pytest.raises(SendError, match="not verified"):
        deliver(db, did, client=resend.client, mode="LIVE")
    assert db.get_draft(did)["status"] == "approved"


def test_missing_resend_key_blocks_before_any_request(db, mail_env, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY")
    resend = FakeResend()
    with pytest.raises(SendBlocked, match="RESEND_API_KEY"):
        deliver(db, _approved_draft(db), client=resend.client, mode="TEST")
    assert resend.calls == []


# 7 ------------------------------------------------------------------
def _rss(title, when):
    return ("<rss version='2.0'><channel><title>n</title><item><title>%s</title>"
            "<link>https://news.example/a1</link><pubDate>%s</pubDate></item></channel></rss>"
            % (title, when.strftime("%a, %d %b %Y %H:%M:%S GMT")))


def test_confirmed_negative_news_vetoes_company(db):
    db.upsert_company("acmeflow.io")
    db.add_fact("acmeflow.io", "is_b2b_saas", "true", "https://acmeflow.io/", "Book a demo")
    db.add_fact("acmeflow.io", "grc_job", "Head of GRC", "https://acmeflow.io/careers", "Head of GRC")
    recent = datetime.now(timezone.utc) - timedelta(days=30)
    fetch = lambda url: _rss("Acmeflow confirms data breach affecting customers", recent)
    confirm_all = lambda domain, events: list(range(len(events)))  # the judge agrees these are about Acmeflow
    ids = news.check_negative_news(db, "acmeflow.io", "Acmeflow", 365, fetch=fetch, sleep=lambda s: None,
                                   judge=confirm_all)
    assert ids
    q = qualify_company(Services(db=db, cfg=ICP), "acmeflow.io")
    assert q.decision == "NO_GO" and "negative news" in q.reasons[0].text.lower()
    assert not q.passes_threshold


def test_old_or_other_company_news_does_not_veto():
    old = datetime.now(timezone.utc) - timedelta(days=900)
    assert news.negative_events(_rss("Acmeflow data breach", old), "Acmeflow", 365) == []
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    assert news.negative_events(_rss("Globex data breach", recent), "Acmeflow", 365) == []
    assert news.negative_events(_rss("Acmeflow launches new product", recent), "Acmeflow", 365) == []


# 8 ------------------------------------------------------------------
def test_go_flagged_email_never_mentions_unknown_signal(db):
    # stage unknown -> GO_FLAGGED with 'stage_fit' unknown; the checker rejects any stage/funding mention
    facts = {"F2": _facts_row("F2", "grc_job", "Head of GRC", "Head of GRC")}
    d = _draft([DraftSentence(text="Saw you're hiring a Head of GRC.", type="hook", fact_ids=["F2"]),
                DraftSentence(text="Teams at your stage often hit audit prep early; is it on your plate?",
                              type="pain", fact_ids=["F2"])])
    r = check_draft(d, _poc(), facts, {}, ["stage_fit"], ["Acmeflow"])
    assert r.passed, r.failures  # "stage" alone is fine; no stage-specific claim
    d2 = _draft([DraftSentence(text="Saw you're hiring a Head of GRC.", type="hook", fact_ids=["F2"]),
                 DraftSentence(text="Series B teams often hit audit prep early; is it on your plate?",
                               type="pain", fact_ids=["F2"])])
    r2 = check_draft(d2, _poc(), facts, {}, ["stage_fit"], ["Acmeflow"])
    assert not r2.passed and any("stage_fit" in f for f in r2.failures)


def test_unknown_facts_never_reach_the_drafting_prompt(db):
    from outreach.draft import fact_payload
    db.upsert_company("x.io")
    db.add_fact("x.io", "grc_job", "Head of GRC", "https://x.io/careers", "Head of GRC")
    db.add_fact("x.io", "generic_inbox", "info@x.io", "https://x.io/", "info@x.io")
    db.add_fact("x.io", "competitor_tool", "Vanta", "https://x.io/security", "We use Vanta")
    db.add_fact("x.io", "negative_news", "breach (2026-01-01)", "https://n/", "x breach")
    fields = {f["field"] for f in fact_payload(db.facts("x.io"))}
    assert fields == {"grc_job"}  # no inbox, competitor or news facts; no unknown fields exist at all


# 9 ------------------------------------------------------------------
def test_discovered_company_without_sourceable_domain_is_dropped():
    text = "Fintech startup Globex raised money. Also Initech announced a launch at https://initech.com/news"
    links = ["https://initech.com/news", "https://techcrunch.com/x"]
    cands = [DiscoveredCompany(company_name="Globex", domain="globex.com"),       # not in text/links
             DiscoveredCompany(company_name="Initech", domain="initech.com"),     # in text + links
             DiscoveredCompany(company_name="Hooli", domain="hooli.xyz"),         # guessed
             DiscoveredCompany(company_name="TC", domain="techcrunch.com")]       # not a company
    kept = verify_discovered(cands, text, links)
    assert [c.domain for c in kept] == ["initech.com"]

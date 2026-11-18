"""No-input mode: Gemini+search only proposes leads; code verifies each against the company's own homepage."""
from __future__ import annotations

from conftest import EMPTY_RSS, make_fetcher
from outreach import config
from outreach.discovery import icp_search, parse_leads, read_inputs, resolve_names
from outreach.models import ExtractionOut
from outreach.pipeline import Services, run_pipeline

OK = lambda d: "1.2.3.4"


class SearchLLM:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def grounded_search(self, prompt):
        self.prompts.append(prompt)
        return self.text

    def generate_json(self, prompt, schema, system=None):  # extraction in the pipeline test
        return ExtractionOut()


LEADS = "Acmeflow | acmeflow.io\nGhostco | ghostco.io\nWrongname | acmeflow.io\nNo Domain Inc | \n"


def test_only_verified_leads_survive(db):
    llm = SearchLLM(LEADS)
    found = icp_search(db, llm, make_fetcher(db), config.icp(), target=10, max_queries=1, resolver=OK)
    assert [f["domain"] for f in found] == ["acmeflow.io"]  # ghostco 404s, 'Wrongname' not on page, blank dropped
    row = db.one("SELECT origin, source_url FROM companies WHERE domain='acmeflow.io'")
    assert row["origin"] == "icp_search" and row["source_url"].startswith("https://acmeflow.io")


def test_non_resolving_domain_is_dropped(db):
    def nxdomain(d):
        raise OSError("nope")
    assert icp_search(db, SearchLLM(LEADS), make_fetcher(db), config.icp(), 5, 1, nxdomain) == []


def test_queries_come_from_the_icp_and_prompt_forbids_guessing(db):
    llm = SearchLLM("")
    icp_search(db, llm, make_fetcher(db), config.icp(), 5, 2, OK)
    assert "Series A" in llm.prompts[0] and "did not see" in llm.prompts[0]


def test_company_names_resolve_to_verified_domains_or_drop(db):
    llm = SearchLLM("Acmeflow | acmeflow.io")
    assert [r["domain"] for r in resolve_names(db, llm, make_fetcher(db), ["Acmeflow"], OK)] == ["acmeflow.io"]
    bad = SearchLLM("Ghostco | ghostco.io")
    assert resolve_names(db, bad, make_fetcher(db), ["Ghostco"], OK) == []


def test_input_file_splits_domains_and_names(tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("acme.io\nhttps://www.globex.com/\nInitech Systems  # a name\n\n")
    assert read_inputs(f) == (["acme.io", "globex.com"], ["Initech Systems"])


def test_run_with_no_input_discovers_and_reports_shortfall_honestly(db):
    svc = Services(db=db, llm=SearchLLM(LEADS), fetcher=make_fetcher(db), news_fetch=lambda u: EMPTY_RSS,
                   resolver=OK, cfg=config.icp())
    s = run_pipeline(svc, [], min_companies=10)  # asks for 10 good fits
    assert s["companies"] == 1  # only the verified lead was researched
    import re
    assert any(re.search(r"found \d+ of the 10 good-fit", n) and "not padded" in n for n in s["notes"])  # honest shortfall

"""Grounding stays strict (the words must be on the page) but is not fooled by typography.
Found in the first real run: valid B2B facts were discarded because of curly quotes and dashes."""
from __future__ import annotations

from conftest import make_fetcher
from outreach.extract import deterministic_facts
from outreach.scraper import scrape_company
from outreach.util import snippet_in_text


PAGE = "Clay’s platform — built for teams — helps sales teams. **Book a demo** today."


def test_cosmetic_differences_do_not_discard_a_true_snippet():
    assert snippet_in_text("Clay's platform - built for teams - helps sales teams.", PAGE)   # quotes + dashes
    assert snippet_in_text("Book a demo today.", PAGE)                                         # markdown marks
    assert snippet_in_text("...built for teams - helps sales teams", PAGE)                    # model-added ellipsis
    assert snippet_in_text("  CLAY'S   PLATFORM ", PAGE)                                       # case + whitespace


def test_grounding_is_still_strict_about_the_words_themselves():
    assert not snippet_in_text("Clay is the best platform for enterprises", PAGE)             # invented wording
    assert not snippet_in_text("Clay's platform helps finance teams", PAGE)                    # one word changed
    assert not snippet_in_text("", PAGE) and not snippet_in_text("...", PAGE) and not snippet_in_text(None, PAGE)


def test_b2b_is_proven_from_the_pages_own_demo_and_software_cues(db):
    db.upsert_company("acmeflow.io")
    scrape_company(db, "acmeflow.io", make_fetcher(db))
    deterministic_facts(db, "acmeflow.io", [])
    (f, *_) = db.facts("acmeflow.io", "is_b2b_saas")
    assert f["value"] == "true" and "Book a demo" in f["snippet"] and f["source_url"].startswith("https://acmeflow.io")


def test_no_demo_or_sales_cue_means_no_b2b_claim(db):
    db.upsert_company("plain.io")
    db.save_page("plain.io", "https://plain.io/", 200,
                 "Plain is a small studio that makes sourdough bread and sells it at the weekend market.", [], [])
    deterministic_facts(db, "plain.io", [])
    assert db.facts("plain.io", "is_b2b_saas") == []


def test_people_at_other_organizations_are_not_treated_as_employees():
    from outreach.extract import names_other_org
    own = {"tavus"}
    assert names_other_org("VP of Product, Orum", own)               # a customer quoted in a testimonial
    assert names_other_org("CTO at Globex", own) and names_other_org("Investor, Sequoia Capital", own)
    assert not names_other_org("Head of Security", own)               # plain title
    assert not names_other_org("CEO, Co-founder", own)                # role words are not organizations
    assert not names_other_org("Founder, Tavus", own)                 # this company itself

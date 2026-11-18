from __future__ import annotations

import pytest
import yaml

from conftest import FakeLLM
from outreach import config, feedback
from outreach.models import RubricChange, RubricProposalOut
from outreach.qualify import qualify
from outreach.review import record_review

ICP = config.icp()


def _co(db, d="x.io", **facts):
    db.upsert_company(d)
    for field, value in facts.items():
        db.add_fact(d, field, value, f"https://{d}/p", f"snippet {value}")
    return d


def test_unknown_b2b_is_no_go_but_capped_fetch_is_insufficient(db):
    d = _co(db)
    assert qualify(db, d, ICP).decision == "NO_GO"
    assert qualify(db, d, ICP, scrape_status="insufficient_data").decision == "INSUFFICIENT_DATA"


def test_not_b2b_saas_drops(db):
    d = _co(db, is_b2b_saas="false")
    assert qualify(db, d, ICP).decision == "NO_GO"


def test_fully_certified_on_competitor_drops_but_partial_does_not(db):
    d = _co(db, is_b2b_saas="true", cert_soc2_type2="SOC 2 Type II", cert_iso27001="ISO 27001",
            competitor_tool="Vanta")
    assert qualify(db, d, ICP).decision == "NO_GO"
    d2 = _co(db, "y.io", is_b2b_saas="true", cert_soc2_type2="SOC 2 Type II", grc_job="Head of GRC")
    assert qualify(db, d2, ICP).decision in ("GO", "GO_FLAGGED")


def test_clearly_out_of_range_short_circuits(db):
    assert qualify(db, _co(db, "big.io", is_b2b_saas="true", headcount="5,000 employees"),
                   ICP).decision == "NO_GO"
    assert qualify(db, _co(db, "tiny.io", is_b2b_saas="true", funding_stage="Seed",
                           headcount="6 employees"), ICP).decision == "NO_GO"
    assert qualify(db, _co(db, "pub.io", is_b2b_saas="true", funding_stage="Public company"),
                   ICP).decision == "NO_GO"


def test_no_trigger_is_flagged_not_dropped_and_below_threshold_does_not_continue(db):
    q = qualify(db, _co(db, is_b2b_saas="true"), ICP)
    assert q.decision == "GO_FLAGGED" and {"compliance_trigger", "stage_fit"} <= set(q.unknowns)
    assert not q.passes_threshold  # score 0 < send_threshold 3


def test_rubric_loop_never_changes_hard_gates_and_needs_approval(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    (tmp_path / "rubric_v1.yaml").write_text(yaml.safe_dump(
        {"version": 1, "drafting_guidance": ["short"], "soft_weights": {}}))
    db_ = db
    did = db_.save_draft(domain="x.io", subject="s", body="Hi A,\n\nbody", sentences="[]", pain_points="[]",
                         greeting_name="A", rubric_version=1, status="pending_review",
                         check_result='{"passed": true}', poc="{}")
    record_review(db_, did, "edit", "bad", "too_generic", "needs a sharper hook",
                  final_text="Hi A,\n\nsharper body")
    assert db_.get_draft(did)["body"].endswith("sharper body")
    llm = FakeLLM({RubricProposalOut: lambda p: RubricProposalOut(changes=[
        RubricChange(target="drafting_guidance", description="sharper hooks",
                     new_guidance_line="Open with the most specific verified fact."),
        RubricChange(target="soft_weights", description="boost", weight_key="stage_fit", new_weight=3),
        RubricChange(target="soft_weights", description="evil", weight_key="hard_gates", new_weight=0)])})
    path = feedback.propose_rubric(db_, llm, tmp_path / "rubric_proposals")
    prop = yaml.safe_load(path.read_text())
    assert len(prop["changes"]) == 2  # the hard-gate change was filtered in code
    assert not (tmp_path / "rubric_v2.yaml").exists()  # nothing applied without approval
    new = feedback.approve_proposal(db_, path, approved=[0], config_dir=tmp_path)
    v2 = yaml.safe_load(new.read_text())
    assert v2["version"] == 2 and "Open with the most specific verified fact." in v2["drafting_guidance"]
    assert v2["soft_weights"] == {}  # change 1 was not approved
    assert yaml.safe_load((tmp_path / "rubric_v1.yaml").read_text())["version"] == 1  # v1 untouched


def test_review_requires_valid_label_and_tag(db):
    did = db.save_draft(domain="x.io", subject="s", body="b", sentences="[]", pain_points="[]",
                        greeting_name="A", rubric_version=1, status="pending_review",
                        check_result="{}", poc="{}")
    with pytest.raises(ValueError):
        record_review(db, did, "approve", "meh", "other")
    with pytest.raises(ValueError):
        record_review(db, did, "approve", "good", "not_a_tag")

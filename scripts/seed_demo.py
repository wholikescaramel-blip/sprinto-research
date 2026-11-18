"""Creates data/demo.db with FICTIONAL companies so the UI can be built/tested without API calls.
Run the UI on it with:  OUTREACH_DB=data/demo.db uvicorn outreach.api:app   (UI shows a DEMO banner)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from outreach import config  # noqa: E402
from outreach.db import DB  # noqa: E402
from outreach.models import CheckResult, Poc  # noqa: E402
from outreach.qualify import qualify  # noqa: E402

path = Path(__file__).resolve().parents[1] / "data" / "demo.db"
path.parent.mkdir(exist_ok=True)
path.unlink(missing_ok=True)
db = DB(path)
icp = config.icp()


def company(domain, name, facts):
    db.upsert_company(domain, name, f"https://{domain}/", origin="icp_search")
    ids = {}
    for field, value, url, snip in facts:
        ids[(field, value)] = db.add_fact(domain, field, value, f"https://{domain}{url}", snip)
    db.set_stage(domain, "scrape", "done")
    db.set_stage(domain, "extract", "done")
    q = qualify(db, domain, icp)
    db.save_qual(q)
    db.set_stage(domain, "qualify", "done")
    return ids


def draft(domain, poc, ids, sentences, status="pending_review", failures=None):
    db.save_poc(poc)
    db.set_stage(domain, "poc", "done")
    body_parts = [s["text"] for s in sentences]
    body = f"Hi {poc.first_name},\n\n" + "\n\n".join(body_parts) + "\n\nBest,\nKay Dee\nSprinto"
    cr = CheckResult(passed=not failures, failures=failures or [], warnings=[])
    did = db.save_draft(domain=domain, subject=sentences[0].get("subject", "Quick question"), body=body,
                        sentences=json.dumps([{k: v for k, v in s.items() if k != "subject"} for s in sentences]),
                        pain_points=json.dumps([]), greeting_name=poc.first_name, rubric_version=1,
                        status=status, check_result=cr.model_dump_json(), poc=poc.model_dump_json())
    db.set_stage(domain, "draft", "done")
    return did


# 1 ---- demo company with an open GRC role + SOC 2 in progress
a = "acmeflow.example"
ids = company(a, "Acmeflow", [
    ("company_name", "Acmeflow", "/", "Acmeflow is a cloud platform that helps operations teams automate approvals."),
    ("is_b2b_saas", "true", "/", "Book a demo to see it in action."),
    ("grc_job", "Head of GRC", "/careers", "Head of GRC"),
    ("soc2_in_progress", "SOC 2 in progress", "/security",
     "We are currently working toward our SOC 2 Type II audit, with the observation window starting this quarter."),
    ("enterprise_signal", "security questionnaire", "/pricing",
     "a signed Data Processing Agreement and security questionnaire support."),
    ("headcount", "85 employees", "/about", "We are a team of 85 employees working across three time zones."),
])
job, soc = ids[("grc_job", "Head of GRC")], ids[("soc2_in_progress", "SOC 2 in progress")]
draft(a, Poc(domain=a, status="ok", name="Priya Nair", title="Head of Security", email="priya@acmeflow.example",
             email_status="sourced", source="hunter", confidence=92, verification="valid",
             role_type="security_head", reason="Hunter: best role match (security_head; confidence 92)"), ids,
      [{"subject": "Your Head of GRC opening", "text": "Saw Acmeflow is hiring a Head of GRC.", "type": "hook", "fact_ids": [job]},
       {"text": "With the SOC 2 Type II audit window opening, is evidence collection landing on your team?", "type": "pain", "fact_ids": [soc]},
       {"text": "Sprinto monitors your controls around the clock and, when something drifts, closes gaps, refreshes evidence and routes approvals.", "type": "pitch", "fact_ids": ["S2"]},
       {"text": "Open to a quick chat this week?", "type": "cta", "fact_ids": []}], "pending_review")

# 2 ---- demo company selling to enterprises
b = "northwind-labs.example"
ids = company(b, "Northwind Labs", [
    ("company_name", "Northwind Labs", "/", "Northwind Labs builds analytics software for finance teams."),
    ("is_b2b_saas", "true", "/", "Per-seat pricing for finance teams. Book a demo."),
    ("enterprise_signal", "trust center", "/trust", "Visit our trust center for our latest security documents."),
    ("funding_stage", "Series B", "/about", "Northwind Labs raised a Series B round led by Fjord Capital."),
    ("eu_regulated", "GDPR", "/privacy", "We process personal data in line with the GDPR."),
])
ent = ids[("enterprise_signal", "trust center")]
draft(b, Poc(domain=b, status="ok", name="Marcus Lee", title="CTO", email="marcus@northwind-labs.example",
             email_status="verified", source="apollo_manual", role_type="cto_vp_eng",
             reason="Manual Apollo lookup from config/manual_pocs.yaml"), ids,
      [{"subject": "Northwind's trust center", "text": "Noticed Northwind Labs runs a public trust center.", "type": "hook", "fact_ids": [ent]},
       {"text": "Are security questionnaires from larger buyers pulling engineers off product work?", "type": "pain", "fact_ids": [ent]},
       {"text": "Sprinto lets you launch a Trust Center and complete security questionnaires in minutes.", "type": "pitch", "fact_ids": ["S5"]},
       {"text": "Worth a short call?", "type": "cta", "fact_ids": []}], "pending_review")

# 3 ---- a blocked draft (checker caught an unsupported claim)
c = "tidalwork.example"
ids = company(c, "Tidalwork", [
    ("company_name", "Tidalwork", "/", "Tidalwork is scheduling software for clinics."),
    ("is_b2b_saas", "true", "/", "Request a demo for your clinic group."),
    ("grc_job", "Security & Compliance Manager", "/careers", "Security & Compliance Manager"),
    ("headcount", "140 employees", "/about", "Our 140 employees support clinics in 12 countries."),
])
draft(c, Poc(domain=c, status="ok", name="Ana Ruiz", title="Head of Compliance", email="ana@tidalwork.example",
             email_status="sourced", source="hunter", confidence=81, role_type="compliance_grc",
             reason="Hunter: best role match (compliance_grc; confidence 81)"), ids,
      [{"subject": "Congrats on the Series C", "text": "Congrats on the Series C and the hiring push.", "type": "hook",
        "fact_ids": [ids[("grc_job", "Security & Compliance Manager")]]},
       {"text": "Is audit prep slow?", "type": "pain", "fact_ids": [ids[("grc_job", "Security & Compliance Manager")]]}],
      "blocked", failures=["sentence 1 (hook): unsupported claim(s) [\"funding claim 'Series C'\"]"])

# 4 ---- a NO_GO company
d = "globex-consumer.example"
company(d, "Globex", [
    ("company_name", "Globex", "/", "Globex makes budgeting apps for families."),
    ("is_b2b_saas", "false", "/", "Download our free app for your household."),
])
print("demo db written:", path)

"""Pydantic schemas shared across stages."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Fact(BaseModel):
    id: str
    domain: str
    field: str
    value: str
    source_url: str
    snippet: str
    fetched_at: str


# ---- Extraction (LLM output, strict) -------------------------------------
class FieldOut(BaseModel):
    value: Optional[str] = None
    source_url: Optional[str] = None
    snippet: Optional[str] = None


class PersonOut(BaseModel):
    name: str
    title: str
    source_url: str
    snippet: str


class ExtractionOut(BaseModel):
    company_name: Optional[FieldOut] = None
    product_description: Optional[FieldOut] = None
    is_b2b_saas: Optional[FieldOut] = None  # value "true" / "false"
    funding_stage: Optional[FieldOut] = None
    headcount: Optional[FieldOut] = None
    eu_or_regulated_customers: Optional[FieldOut] = None
    named_customers: list[FieldOut] = Field(default_factory=list)
    people: list[PersonOut] = Field(default_factory=list)


class NewsJudgeOut(BaseModel):
    about_company: list[int] = Field(default_factory=list)  # indices of headlines about THIS company


# ---- Discovery -----------------------------------------------------------
class DiscoveredCompany(BaseModel):
    company_name: str
    domain: str


class DiscoveryOut(BaseModel):
    companies: list[DiscoveredCompany] = Field(default_factory=list)


# ---- Qualification -------------------------------------------------------
Decision = Literal["GO", "GO_FLAGGED", "NO_GO", "INSUFFICIENT_DATA"]


class Reason(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)


class QualResult(BaseModel):
    domain: str
    decision: Decision
    score: float = 0
    confidence: str = "low"
    reasons: list[Reason] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    passes_threshold: bool = False
    rubric_version: int = 1


# ---- PoC -----------------------------------------------------------------
class Poc(BaseModel):
    domain: str
    status: Literal["ok", "needs_human_email", "no_named_poc"]
    name: Optional[str] = None
    title: Optional[str] = None
    email: Optional[str] = None
    # verified | sourced | unverified  (see poc.py)
    email_status: str = "unverified"
    source: Optional[str] = None  # hunter | apollo_manual | company_page | generic_inbox
    confidence: Optional[int] = None
    verification: Optional[str] = None
    role_type: Optional[str] = None  # security_head | cto_vp_eng | compliance_grc | founder_ceo
    reason: str = ""
    guessed_email: Optional[str] = None  # suggestion for a human only; never sent

    @property
    def first_name(self) -> Optional[str]:
        return self.name.split()[0] if self.name else None


# ---- Drafting ------------------------------------------------------------
class PainPick(BaseModel):
    pain_id: str
    rationale: str
    fact_ids: list[str] = Field(default_factory=list)


class PainPicksOut(BaseModel):
    pain_points: list[PainPick] = Field(default_factory=list)


class DraftSentence(BaseModel):
    text: str
    type: Literal["hook", "pain", "pitch", "cta", "other"]
    fact_ids: list[str] = Field(default_factory=list)


class DraftOut(BaseModel):
    subject: str
    greeting_name: str
    sentences: list[DraftSentence]


class CheckResult(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---- Rubric proposals ------------------------------------------------------
class RubricChange(BaseModel):
    target: Literal["drafting_guidance", "soft_weights"]
    description: str
    new_guidance_line: Optional[str] = None
    weight_key: Optional[str] = None
    new_weight: Optional[float] = None


class RubricProposalOut(BaseModel):
    changes: list[RubricChange] = Field(default_factory=list)

"""Email delivery via Resend (https://resend.com/docs/api-reference/emails/send-email).

TEST: send to MY_EMAIL only, subject prefixed [TEST], body starts with the intended recipient.
LIVE: send to the real recipient. Only ever after a human approved that exact draft; never automatic.
The public hosted app (PUBLIC_MODE=1) is forced to TEST: it can never email a third party.
"""
from __future__ import annotations

import httpx

from . import config
from .db import DB
from .models import CheckResult, Poc

RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Outreach <onboarding@resend.dev>"  # Resend's shared test sender


class SendBlocked(Exception):
    """A safety gate said no. Nothing was sent."""


class SendError(Exception):
    """Resend rejected or failed the request."""


def public_mode() -> bool:
    return config.env("PUBLIC_MODE") == "1"


def check_sendable(draft, poc: Poc | None) -> None:
    """Hard gates, applied in both modes. Raises SendBlocked with the reason."""
    if draft["status"] not in ("approved", "edited"):
        raise SendBlocked(f"draft status is '{draft['status']}'; it must be approved in review first")
    cr = CheckResult.model_validate_json(draft["check_result"] or '{"passed": false}')
    if not cr.passed:
        raise SendBlocked("claim checker did not pass")
    if poc is None or poc.status == "no_named_poc" or not poc.name:
        raise SendBlocked("no named PoC")
    if poc.email_status == "unverified" or not poc.email:
        raise SendBlocked("PoC email is unverified (or missing); a human must supply a verified address")


def build_payload(draft, poc: Poc, mode: str, my_email: str | None, sender: str) -> dict:
    if mode == "TEST":
        if not my_email:
            raise SendBlocked("MY_EMAIL is not set")
        return {"from": sender, "to": [my_email], "subject": f"[TEST] {draft['subject']}",
                "text": f"Intended recipient: {poc.name} <{poc.email}>\n\n{draft['body']}"}
    payload = {"from": sender, "to": [poc.email], "subject": draft["subject"], "text": draft["body"]}
    if my_email:
        payload["reply_to"] = [my_email]  # replies reach you, not the shared sender
    return payload


def deliver(db: DB, draft_id: int, client: httpx.Client | None = None, mode: str | None = None) -> dict:
    mode = (mode or config.send_mode()).upper()
    if public_mode():
        mode = "TEST"  # a public, password-less app must never be able to email strangers
    draft = db.get_draft(draft_id)
    if draft is None:
        raise SendBlocked(f"no draft {draft_id}")
    poc = Poc.model_validate_json(draft["poc"]) if draft["poc"] else db.get_poc(draft["domain"])
    check_sendable(draft, poc)
    key = config.env("RESEND_API_KEY")
    if not key:
        raise SendBlocked("RESEND_API_KEY is not set")
    payload = build_payload(draft, poc, mode, config.env("MY_EMAIL"),
                            config.env("MAIL_FROM", DEFAULT_FROM))
    c = client or httpx.Client(timeout=20)
    try:
        r = c.post(RESEND_URL, json=payload, headers={
            "Authorization": f"Bearer {key}",
            "Idempotency-Key": f"outreach-draft-{draft_id}-{mode}"})  # a double click cannot double-send
    except httpx.HTTPError as e:
        raise SendError(f"could not reach Resend: {e}") from e
    if r.status_code >= 300:
        try:
            msg = r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise SendError(f"Resend said {r.status_code}: {str(msg)[:300]}")
    message_id = r.json().get("id")
    status = "sent_test" if mode == "TEST" else "sent_live"
    db.update_draft(draft_id, status=status, message_id=message_id, sent_mode=mode)
    return {"mode": mode, "message_id": message_id, "status": status, "to": payload["to"]}

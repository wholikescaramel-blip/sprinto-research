"""Gmail delivery. TEST: send to MY_EMAIL. LIVE: create a draft only - never auto-send."""
from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from pathlib import Path

from . import config
from .db import DB
from .models import CheckResult, Poc

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


class SendBlocked(Exception):
    pass


def gmail_service():
    """OAuth (installed-app flow). Token cached next to the credentials file."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    cred_file = Path(config.env("GMAIL_CREDENTIALS_FILE", "credentials.json"))
    if not cred_file.is_absolute():
        cred_file = config.ROOT / cred_file
    token_file = Path(config.env("GMAIL_TOKEN_FILE") or cred_file.with_name("token.json"))
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cred_file.exists():
                raise SendBlocked(f"Gmail credentials file not found: {cred_file}")
            creds = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES).run_local_server(port=0)
        token_file.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


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


def build_message(draft, poc: Poc, mode: str, my_email: str | None) -> EmailMessage:
    msg = EmailMessage()
    if mode == "TEST":
        if not my_email:
            raise SendBlocked("MY_EMAIL is not set")
        msg["To"] = my_email
        msg["Subject"] = f"[TEST] {draft['subject']}"
        msg.set_content(f"Intended recipient: {poc.name} <{poc.email}>\n\n{draft['body']}")
    else:
        msg["To"] = poc.email
        msg["Subject"] = draft["subject"]
        msg.set_content(draft["body"])
    return msg


def deliver(db: DB, draft_id: int, service=None, mode: str | None = None) -> dict:
    """TEST -> users.messages.send to MY_EMAIL. LIVE -> users.drafts.create (never messages.send)."""
    mode = (mode or config.send_mode()).upper()
    draft = db.get_draft(draft_id)
    if draft is None:
        raise SendBlocked(f"no draft {draft_id}")
    poc = db.get_poc(draft["domain"])
    poc_at_draft = Poc.model_validate_json(draft["poc"]) if draft["poc"] else poc
    check_sendable(draft, poc_at_draft)
    msg = build_message(draft, poc_at_draft, mode, config.env("MY_EMAIL"))
    svc = service or gmail_service()
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    if mode == "TEST":
        resp = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        status = "sent_test"
    else:
        resp = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        status = "gmail_draft_created"
    db.update_draft(draft_id, status=status, gmail_id=resp.get("id"), sent_mode=mode)
    return {"mode": mode, "gmail_id": resp.get("id"), "status": status}

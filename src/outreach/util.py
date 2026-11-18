"""Small shared helpers."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import tldextract

# Offline: use the bundled public-suffix snapshot, never hit the network.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def registrable_domain(value: str) -> str | None:
    """'https://www.app.acme.co.uk/x' -> 'acme.co.uk'. None if it isn't a valid domain."""
    v = (value or "").strip().lower()
    if not v:
        return None
    if "//" in v:
        v = urlparse(v).netloc or ""
    v = v.split("/")[0].split(":")[0].lstrip(".")
    if not _DOMAIN_RE.match(v):
        return None
    ext = _extract(v)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def snippet_in_text(snippet: str | None, text: str) -> bool:
    """Whitespace/case-insensitive substring check. Empty snippets never pass."""
    if not snippet or not norm_ws(snippet):
        return False
    return norm_ws(snippet).casefold() in norm_ws(text).casefold()

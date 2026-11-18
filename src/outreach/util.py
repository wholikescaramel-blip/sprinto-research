"""Small shared helpers."""
from __future__ import annotations

import re
import unicodedata
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


_TYPO = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "−": "-", " ": " ", "​": "", "‌": "", "‍": "", "﻿": ""})
_MARKUP = re.compile(r"[*_`#>]+")


_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask_emails(o):
    """'priya@acme.com' -> 'p***@acme.com' anywhere in a str / list / dict (used on public pages and in samples)."""
    if isinstance(o, str):
        return _EMAIL_RE.sub(lambda m: m.group(1) + "***" + m.group(2), o)
    if isinstance(o, list):
        return [mask_emails(x) for x in o]
    if isinstance(o, dict):
        return {k: mask_emails(v) for k, v in o.items()}
    return o


def norm_ws(s: str) -> str:
    """Whitespace + typography normalisation, applied identically to the snippet and the page text, so a model
    that returns straight quotes for curly ones (or drops **bold** marks) is not penalised. It never invents words."""
    s = unicodedata.normalize("NFKC", s or "").translate(_TYPO)
    return re.sub(r"\s+", " ", _MARKUP.sub("", s)).strip()


def snippet_in_text(snippet: str | None, text: str) -> bool:
    """Case/whitespace/typography-insensitive SUBSTRING check. Empty snippets never pass.
    A trailing or leading ellipsis the model added to mark truncation is ignored; the words themselves must match."""
    if not snippet or not norm_ws(snippet):
        return False
    s = norm_ws(snippet).strip(" .…")
    return bool(s) and s.casefold() in norm_ws(text).casefold()

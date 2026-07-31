"""Parse inviter mid from bare id or full invite deep-link URL."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# e.g. GNWPX5251 / MSRDX7919
_MID_RE = re.compile(r"\b([A-Za-z]{3,8}\d{3,6})\b")


def parse_inviter(value: str) -> str:
    """Accept mid or full URL/text; return inviter mid string.

    Examples:
      GNWPX5251
      https://crumble.onelink.me/cfu9/xxx?deep_link_action=invite&deep_link_value=GNWPX5251
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("inviter is empty")

    # strip wrapping quotes / whitespace
    raw = raw.strip().strip("\"'")

    # If it looks like a URL or query blob, parse deep_link_value first
    if any(tok in raw for tok in ("://", "deep_link", "onelink", "?", "invite")):
        mid = _from_url_or_query(raw)
        if mid:
            return mid

    # Bare mid
    if re.fullmatch(r"[A-Za-z0-9]{6,20}", raw):
        return raw

    m = _MID_RE.search(raw)
    if m:
        return m.group(1)

    raise ValueError(f"cannot parse inviter mid from: {value!r}")


def _from_url_or_query(raw: str) -> str | None:
    text = raw
    if "://" not in text and "deep_link" in text:
        # query-only fragment
        qs = parse_qs(text.lstrip("?"), keep_blank_values=False)
    else:
        parsed = urlparse(text)
        qs = parse_qs(parsed.query, keep_blank_values=False)
        if parsed.fragment:
            frag = parsed.fragment
            if "=" in frag:
                # fragment may itself be a query
                qs = {**parse_qs(frag, keep_blank_values=False), **qs}
            # AppsFlyer sometimes nests af_dp / deep link URL in a param
        for nest_key in ("af_dp", "deep_link", "link", "af_web_dp", "af_r"):
            if nest_key in qs and qs[nest_key]:
                nested = unquote(qs[nest_key][0])
                if "deep_link_value" in nested or "://" in nested:
                    got = _from_url_or_query(nested)
                    if got:
                        return got

    for key in (
        "deep_link_value",
        "deep_link_sub1",
        "inviter",
        "inviter_mid",
        "mid",
        "invite_code",
        "code",
        "c",
    ):
        if key in qs and qs[key]:
            cand = unquote(qs[key][0]).strip()
            if cand:
                # value itself might still be a URL
                if "://" in cand or "deep_link" in cand:
                    nested = _from_url_or_query(cand)
                    if nested:
                        return nested
                m = _MID_RE.search(cand)
                return m.group(1) if m else cand

    # fallback: scan whole string for mid-like token
    m = _MID_RE.search(raw)
    return m.group(1) if m else None

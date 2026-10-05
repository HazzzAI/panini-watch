"""Compare: find one product in every shop, group the listings into *versions* (e.g. "Box · 100 packs"),
and rank each version cheapest-first in USD."""
from __future__ import annotations

import re

from . import football

_STOP = {"the", "a", "an", "of", "and", "di", "de", "la", "el", "il", "le", "les", "der", "die", "das", "-"}


def norm_cmp(s: str) -> str:
    """Accent/case-insensitive text with season spellings unified (2024/25, 2024-2025 -> 2024-25)."""
    s = football._norm(s)
    s = re.sub(r"(\d{4})\s*[-/–—]\s*(\d{2,4})", lambda m: f"{m.group(1)}-{m.group(2)[-2:]}", s)
    return re.sub(r"[^a-z0-9\-]+", " ", s).strip()


def rough_text(name: str) -> str:
    from .glossary import rough
    return rough(name)


def tokens(term: str) -> list[str]:
    return [t for t in norm_cmp(term).split() if t not in _STOP]


def match_score(toks: list[str], *texts: str) -> int:
    """How many query words a listing contains (looked up in its original AND English names)."""
    blob = " " + " ".join(norm_cmp(t) for t in texts if t) + " "
    return sum(1 for t in toks if t in blob)


# ---------------------------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------------------------
_COUNT = [
    r"\b(?:box|case|display|tin|blister)\s+of\s+(\d{1,3})\b",
    r"\b(\d{1,3})\s*[- ]?\s*(?:count|ct)\b",
    r"\b(\d{1,3})\s*(?:x\s*)?(?:packs?|packets?|sachets?|boosters?|pouches)\b",
    r"\bpacks?\s*(?:of\s*)?x?\s*(\d{1,3})\b",
    r"\bx\s*(\d{1,3})\s*packs?\b",
]
_BOX = r"\b(box|boxes|display|case|cajita|caja|scatola|boite|carton|cutie|doboz|blaster)\b"


def _plain(name: str) -> str:
    from .glossary import rough
    return football._norm(rough(name))          # "Cajita 100 sobres" -> "box 100 packs"


def pack_count(name: str) -> int | None:
    t = _plain(name)
    for pat in _COUNT:
        m = re.search(pat, t)
        if m and 1 <= int(m.group(1)) <= 300:
            return int(m.group(1))
    return None


def variant_of(name: str) -> tuple[str, str]:
    """-> (label, icon). Pure box vs bundle vs album ... plus the number of packs when it is stated."""
    t = _plain(name)
    n = pack_count(name)
    has_box = re.search(_BOX, t) is not None
    has_album = re.search(r"\b(album|albums|booklet|binder|hardback|hardcover)\b", t) is not None
    extras = re.search(r"\+|\bwith\b|\bplus\b|\bcollector|\bbundle\b|\bset\b|\bkit\b", t) is not None
    if re.search(r"\bstarter\b", t):
        base, icon = "Starter pack", "🎁"
    elif re.search(r"\btin\b", t):
        base, icon = "Tin box", "🥫"
    elif (has_box or n) and (has_album or extras) and re.search(r"\+|\bwith\b|\bplus\b|\bbundle\b|\bset\b|\bkit\b|\bcollector", t):
        base, icon = "Bundle", "🎁"
    elif has_box:
        base, icon = "Box", "📦"
    elif re.search(r"\bblisters?\b", t):
        base, icon = "Blister", "🗂"
    elif has_album:
        base, icon = "Album", "📘"
    elif re.search(r"\b(packs?|packets?|sachets?|megapack)\b", t):
        base, icon = "Packs", "🃏"
    elif re.search(r"\bposter\b", t):
        base, icon = "Poster", "🖼"
    elif re.search(r"\bset\b", t):
        base, icon = "Set", "🎁"
    else:
        base, icon = "Standard edition", "🔹"
    if n and base in ("Box", "Bundle", "Blister", "Starter pack", "Tin box", "Packs", "Album"):
        return f"{base} · {n} packs", icon
    return base, icon


# ---------------------------------------------------------------------------------------------
# prices
# ---------------------------------------------------------------------------------------------
def usd_value(site_key: str, price: float | None) -> float | None:
    from . import models
    if price is None:
        return None
    iso = models.SITE_ISO.get(site_key, "")
    if models.FX is not None and iso:
        v = models.FX.to_usd(price, iso)
        if v is not None:
            return v
    return price if iso in ("", "USD") else None


def rank_key(row: dict) -> tuple:
    """In-stock first, then cheapest USD; sold-out listings go to the end (still cheapest first)."""
    usd = row.get("usd")
    return (not row["stock"], usd if usd is not None else 1e12)

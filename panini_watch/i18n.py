"""Translate product / category names to English (free Google endpoint + local cache + glossary fixes).

* Brand and collection words (Megacracks, Este, Adrenalyn, LaLiga, Panini …) are protected so they are
  never "translated" (e.g. Spanish "Este" would otherwise become "This").
* Every translation is cached in SQLite, so each distinct name is translated only once, ever.
* If the translation service is unreachable the original text is used and retried later.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from .store import Store

log = logging.getLogger("panini.i18n")

_ENDPOINT = "https://translate.googleapis.com/translate_a/single"

# Words that must survive translation untouched (case-insensitive, matched as whole words).
PROTECTED = [
    "Panini", "Megacracks", "Mega Cracks", "Liga Este", "Adrenalyn XL", "Adrenalyn", "FIFA", "FIFA 365",
    "LaLiga", "La Liga", "Hypermotion", "Calciatori", "Bundesliga", "Eredivisie", "Top Class", "Match Attax",
    "Prizm", "Select", "Donruss", "Topps", "Luminance", "Instant", "Wow Box", "Tin Box", "Megapack", "Blister",
    "UEFA", "Champions League", "Libertadores", "Conmebol", "Copa America", "Futebol", "Brasileirao",
    "Premier League", "Ligue 1", "Serie A", "Fan's Favourites", "Platinum",
]

# Fixes for words Google tends to translate wrongly in a trading-card shop context.
_FIXES = [
    (r"\benvelopes?\b", "packs"), (r"\bsachets?\b", "packs"), (r"\bpouch(es)?\b", "packs"),
    (r"\bbustine?\b", "packs"), (r"\bpochettes?\b", "packs"), (r"\bsobres\b", "packs"),
    (r"\bfigurines?\b", "stickers"), (r"\bstickers? album\b", "sticker album"),
    (r"\bcrackers?\b", "cracks"), (r"\bchromos?\b", "stickers"), (r"\bcromos?\b", "stickers"),
    (r"\bestampas?\b", "stickers"), (r"\bfiguritas?\b", "stickers"), (r"\bfigurinhas?\b", "stickers"),
    (r"\bbags\b", "packs"), (r"\bpockets\b", "packs"), (r"\bbooster packs\b", "packs"),
    (r"^Letters\b", "Cards"), (r"\bhard ?cover booklet\b", "hardcover album"),
]


def _protect(text: str) -> tuple[str, dict[str, str]]:
    slots: dict[str, str] = {}
    out = text

    def _este(m):
        key = f"ZXQESTEZ{len(slots)}"
        slots[key] = m.group(0)
        return key
    # "ESTE" in capitals is the Spanish sticker line; lower/title-case "este" means "this"
    out = re.sub(r"(?<![\w])ESTE(?![\w])", _este, out)
    for i, term in enumerate(sorted(PROTECTED, key=len, reverse=True)):
        pat = re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.I)

        def sub(m, i=i):
            key = f"ZXQ{i}Z{len(slots)}"
            slots[key] = m.group(0)
            return key
        out = pat.sub(sub, out)
    return out, slots


def _restore(text: str, slots: dict[str, str]) -> str:
    for key, original in slots.items():
        text = re.sub(re.escape(key), original, text, flags=re.I)
    return text


def _clean(text: str) -> str:
    for pat, rep in _FIXES:
        text = re.sub(pat, rep, text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


class Translator:
    def __init__(self, store: Store | None = None):
        self.store = store
        self._http = httpx.AsyncClient(timeout=25)
        self._mem: dict[tuple[str, str], str] = {}
        self._sem = asyncio.Semaphore(1)
        self._down_until = 0.0
        self._mm_down_until = 0.0
        self._mm_calls: list[float] = []

    async def close(self) -> None:
        await self._http.aclose()

    def is_down(self) -> bool:
        import time
        return time.time() < self._down_until

    # -- cache -------------------------------------------------------------------------------
    def _cached(self, text: str, lang: str) -> str | None:
        hit = self._mem.get((text, lang))
        if hit is None and self.store:
            hit = self.store.tr_get(text, lang)
            if hit is not None:
                self._mem[(text, lang)] = hit
        return hit

    def _remember(self, text: str, lang: str, result: str) -> None:
        self._mem[(text, lang)] = result
        if self.store:
            self.store.tr_set(text, lang, result)

    # -- network -----------------------------------------------------------------------------
    async def _gtx(self, joined: str, sl: str, tl: str) -> str | None:
        import time
        async with self._sem:
            if time.time() < self._down_until:             # re-check inside the lock (parallel callers)
                return None
            await asyncio.sleep(0.6)                       # stay far below any rate limit
            try:
                r = await self._http.post(_ENDPOINT, data={"client": "gtx", "sl": sl, "tl": tl, "dt": "t",
                                                            "q": joined})
                if r.status_code in (302, 403, 429) or "json" not in r.headers.get("content-type", ""):
                    # the endpoint is asking us to slow down: stop for 30 minutes (never hammer / work around it)
                    self._down_until = time.time() + 1800
                    log.warning("translation endpoint is rate-limiting (HTTP %s): pausing it for 30 min, "
                                "using fallbacks", r.status_code)
                    return None
                return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])
            except Exception as e:
                log.debug("translate failed: %s", e)
                self._down_until = time.time() + 300
                return None

    async def _mymemory(self, text: str, sl: str, tl: str) -> str | None:
        """Second free provider (anonymous quota), used only when the first is paused."""
        import time
        now = time.time()
        if sl == "auto" or now < self._mm_down_until:
            return None
        self._mm_calls = [t for t in self._mm_calls if now - t < 3600]
        if len(self._mm_calls) >= 40:                      # polite hourly budget
            return None
        self._mm_calls.append(now)
        src = {"iw": "he"}.get(sl, sl)
        try:
            r = await self._http.get("https://api.mymemory.translated.net/get",
                                     params={"q": text[:450], "langpair": f"{src}|{tl}"})
            data = r.json()
            out = (data.get("responseData") or {}).get("translatedText", "")
            if str(data.get("responseStatus")) != "200" or not out or "MYMEMORY WARNING" in out.upper():
                self._mm_down_until = now + 6 * 3600
                return None
            return out
        except Exception as e:
            log.debug("mymemory failed: %s", e)
            self._mm_down_until = now + 600
            return None

    async def _call(self, joined: str, sl: str, tl: str) -> str | None:
        out = await self._gtx(joined, sl, tl)
        if out is not None:
            return out
        parts = []
        for line in joined.split("\n"):
            o = await self._mymemory(line, sl, tl)
            if o is None:
                return None
            parts.append(o)
        return "\n".join(parts)

    async def many(self, texts: list[str], *, source_lang: str = "auto", target: str = "en",
                   strict: bool = False) -> list:
        """Translate texts (order preserved). English-language shops should skip this call.
        strict=True returns None for texts that could not be translated right now (instead of the original)."""
        results: list[str | None] = [None] * len(texts)
        todo: list[int] = []
        for i, t in enumerate(texts):
            t = (t or "").strip()
            if not t or not re.search(r"[^\W\d_]", t):          # empty / only digits & symbols
                results[i] = t
                continue
            hit = self._cached(t, target)
            if hit is not None:
                results[i] = hit
            else:
                todo.append(i)
        # batch the rest (unique texts only)
        uniq = list(dict.fromkeys(texts[i].strip() for i in todo))
        chunks = [uniq[k:k + 12] for k in range(0, len(uniq), 12)]

        async def do(chunk: list[str]) -> None:
            protected = [_protect(t) for t in chunk]
            joined = "\n".join(p for p, _ in protected)
            out = await self._call(joined, source_lang, target)
            parts = out.split("\n") if out is not None else []
            if out is not None and len(parts) != len(chunk):       # segments got merged: do one by one
                parts = []
                for p, _ in protected:
                    o = await self._call(p, source_lang, target)
                    parts.append(o if o is not None else "")
            if not parts:
                return
            for (orig, (_, slots)), tr in zip(zip(chunk, protected), parts):
                if tr.strip():
                    self._remember(orig, target, _clean(_restore(tr, slots)))

        await asyncio.gather(*[do(c) for c in chunks])
        for i in todo:
            t = texts[i].strip()
            results[i] = self._cached(t, target) or (None if strict else self.rough(t))
        return [r if (r is not None or strict) else "" for r in results]

    @staticmethod
    def rough(text: str) -> str:
        """Offline best-effort English (glossary only) for when the online translators are paused."""
        from .glossary import rough
        return _clean(rough(text))

    async def one(self, text: str, **kw) -> str:
        return (await self.many([text], **kw))[0]

    async def to_language(self, text: str, lang: str) -> str:
        """English query -> the shop's language (so a search for 'sealed box' also works on panini.es)."""
        t = text.strip()
        key = f"→{lang}"
        hit = self._cached(t, key)
        if hit is not None:
            return hit
        protected, slots = _protect(t)
        out = await self._call(protected, "en", lang)
        if out is None:
            return t
        res = _restore(out, slots).strip()
        self._remember(t, key, res)
        return res

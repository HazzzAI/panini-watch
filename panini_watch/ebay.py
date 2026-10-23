"""eBay search across all eBay sites (official Browse API, free developer keys).

Only listings that ship to the destination (UAE) are requested; eBay sorts by price + shipping to that
country, every site is searched with the query written in its own language, titles are checked against
what the user meant (ebay_query.judge), and the survivors are ranked by the price delivered, in USD.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass, field
import httpx

from . import ebay_query as EQ

log = logging.getLogger("panini.ebay")

# marketplace -> (language, flag, domain)
MARKETS = {
    "EBAY_US": ("en", "🇺🇸", "ebay.com"), "EBAY_GB": ("en", "🇬🇧", "ebay.co.uk"),
    "EBAY_DE": ("de", "🇩🇪", "ebay.de"), "EBAY_IT": ("it", "🇮🇹", "ebay.it"),
    "EBAY_ES": ("es", "🇪🇸", "ebay.es"), "EBAY_FR": ("fr", "🇫🇷", "ebay.fr"),
    "EBAY_NL": ("nl", "🇳🇱", "ebay.nl"), "EBAY_BE": ("nl", "🇧🇪", "ebay.be"),
    "EBAY_AT": ("de", "🇦🇹", "ebay.at"), "EBAY_CH": ("de", "🇨🇭", "ebay.ch"),
    "EBAY_IE": ("en", "🇮🇪", "ebay.ie"), "EBAY_PL": ("pl", "🇵🇱", "ebay.pl"),
    "EBAY_AU": ("en", "🇦🇺", "ebay.com.au"), "EBAY_CA": ("en", "🇨🇦", "ebay.ca"),
}
COUNTRY_FLAG = {"US": "🇺🇸", "GB": "🇬🇧", "DE": "🇩🇪", "IT": "🇮🇹", "ES": "🇪🇸", "FR": "🇫🇷", "NL": "🇳🇱",
                "BE": "🇧🇪", "AT": "🇦🇹", "CH": "🇨🇭", "IE": "🇮🇪", "PL": "🇵🇱", "AU": "🇦🇺", "CA": "🇨🇦",
                "PT": "🇵🇹", "GR": "🇬🇷", "HU": "🇭🇺", "RO": "🇷🇴", "CZ": "🇨🇿", "JP": "🇯🇵", "CN": "🇨🇳",
                "HK": "🇭🇰", "AE": "🇦🇪", "BR": "🇧🇷", "AR": "🇦🇷", "MX": "🇲🇽", "SE": "🇸🇪", "DK": "🇩🇰",
                "NO": "🇳🇴", "FI": "🇫🇮", "HR": "🇭🇷", "RS": "🇷🇸", "SI": "🇸🇮", "SK": "🇸🇰", "TR": "🇹🇷",
                "IL": "🇮🇱", "SG": "🇸🇬", "MY": "🇲🇾", "TH": "🇹🇭", "LT": "🇱🇹", "LV": "🇱🇻", "BG": "🇧🇬"}


# Which eBay sites are searched for which of your delivery addresses ("*" = every site).
ROUTES = {
    "AE": "*",
    "US": {"EBAY_US", "EBAY_CA"},
    "DE": {"EBAY_DE", "EBAY_AT", "EBAY_CH", "EBAY_IT", "EBAY_ES", "EBAY_FR", "EBAY_NL", "EBAY_BE", "EBAY_IE",
           "EBAY_PL"},
    "GB": {"EBAY_GB", "EBAY_IE"},
}
DEST_LABEL = {"AE": "🇦🇪 UAE", "US": "🇺🇸 USA", "DE": "🇩🇪 Germany", "GB": "🇬🇧 UK"}


def dests_for(market: str, dests: list[str], europe: str = "") -> list[str]:
    """The UAE is checked on every site; the USA on American sites; Germany / UK on European sites."""
    out = []
    for d in dests:
        route = ROUTES.get(d, "*")
        if route == "*" or market in route:
            out.append(d)
    return out


def dest_label(d: str, europe: str = "") -> str:
    return DEST_LABEL.get(d, d)


class EbayNotConfigured(Exception):
    pass


@dataclass
class EbayItem:
    item_id: str
    legacy_id: str
    market: str
    title: str
    url: str
    image: str
    price: float
    currency: str
    ship: float | None                  # None = shipping cost not given (calculated at checkout)
    ship_currency: str
    buying: list[str]
    condition: str
    location: str
    seller_pct: str
    seller_score: int | None
    bids: int | None = None
    home_market: str = ""               # the eBay site the seller listed on (its own currency = true price)
    dest: str = ""                      # the destination this price/shipping was quoted for

    @staticmethod
    def from_api(market: str, d: dict) -> "EbayItem | None":
        price = d.get("price") or d.get("currentBidPrice")
        if "AUCTION" in (d.get("buyingOptions") or []) and d.get("currentBidPrice"):
            price = d["currentBidPrice"]
        if not price:
            return None
        ship, ship_cur = None, price.get("currency", "")
        for opt in d.get("shippingOptions") or []:
            cost = opt.get("shippingCost")
            if cost and cost.get("value") is not None:
                v = float(cost["value"])
                if ship is None or v < ship:
                    ship, ship_cur = v, cost.get("currency", ship_cur)
        seller = d.get("seller") or {}
        return EbayItem(
            item_id=d.get("itemId", ""), legacy_id=str(d.get("legacyItemId") or d.get("itemId", "")), market=market,
            title=d.get("title", ""), url=d.get("itemWebUrl", ""), image=(d.get("image") or {}).get("imageUrl", ""),
            price=float(price["value"]), currency=price.get("currency", ""), ship=ship, ship_currency=ship_cur,
            buying=d.get("buyingOptions") or [], condition=d.get("condition", ""),
            location=(d.get("itemLocation") or {}).get("country", ""),
            seller_pct=str(seller.get("feedbackPercentage", "")), seller_score=seller.get("feedbackScore"),
            bids=d.get("bidCount"), home_market=d.get("listingMarketplaceId", ""))


@dataclass
class Match:
    item: EbayItem
    price_usd: float | None
    ship_usd: float | None
    total_usd: float | None
    score: float
    missing_soft: list[str] = field(default_factory=list)
    note: str = ""
    options: dict = field(default_factory=dict)       # destination -> delivered USD, None = does not ship there
    dest: str = ""                                    # cheapest destination for this listing
    alts: dict = field(default_factory=dict)          # other destinations it ships to -> delivered USD


@dataclass
class SearchOutcome:
    intent: EQ.Intent
    queries: dict                       # market -> [queries]
    listings: int
    matches: list[Match]
    errors: list[str]
    plan: list = field(default_factory=list)       # (site, destination) pairs that were searched


class EbayClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, dest: str = "AE"):
        self.client_id = client_id or os.getenv("EBAY_CLIENT_ID") or ""
        self.client_secret = client_secret or os.getenv("EBAY_CLIENT_SECRET") or ""
        self.base = os.getenv("EBAY_API_BASE", "https://api.ebay.com")
        self.dest = dest
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.AsyncClient(timeout=30)
        self._sem = asyncio.Semaphore(6)
        self._cache: dict[tuple, tuple[float, list]] = {}
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def close(self) -> None:
        await self._http.aclose()

    async def token(self) -> str:
        if not self.configured:
            raise EbayNotConfigured()
        async with self._token_lock:                 # one login shared by all parallel searches
            if self._token and time.time() < self._token_exp - 120:
                return self._token
            return await self._login()

    async def _login(self) -> str:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = await self._http.post(f"{self.base}/identity/v1/oauth2/token",
                                  headers={"Authorization": f"Basic {basic}",
                                           "Content-Type": "application/x-www-form-urlencoded"},
                                  data={"grant_type": "client_credentials",
                                        "scope": "https://api.ebay.com/oauth/api_scope"})
        if r.status_code != 200:
            raise RuntimeError(f"eBay login failed ({r.status_code}): {r.text[:200]}")
        d = r.json()
        self._token, self._token_exp = d["access_token"], time.time() + int(d.get("expires_in", 7200))
        return self._token

    async def ship_quote(self, item_id: str, market: str, dest: str) -> tuple[float, str] | None:
        """Cheapest shipping of one listing to `dest` -> (cost, currency); None if the seller does not ship there."""
        key = ("ship", item_id, dest)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 1800:
            return hit[1]
        for attempt in range(3):
            headers = {"Authorization": f"Bearer {await self.token()}", "X-EBAY-C-MARKETPLACE-ID": market,
                       "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3D{dest}"}
            async with self._sem:
                r = await self._http.get(f"{self.base}/buy/browse/v1/item/{item_id}", headers=headers)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            best = None
            for opt in r.json().get("shippingOptions") or []:
                cost = opt.get("shippingCost") or {}
                if cost.get("value") is not None:
                    v = float(cost["value"])
                    if best is None or v < best[0]:
                        best = (v, cost.get("currency", ""))
            self._cache[key] = (time.time(), best)
            return best
        return None

    async def search(self, market: str, q: str, *, limit: int = 100, auctions: bool = False,
                     dest: str | None = None) -> list[EbayItem]:
        dest = dest or self.dest
        key = (market, q, limit, auctions, dest)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        filt = f"deliveryCountry:{dest}"
        if auctions:
            filt += ",buyingOptions:{FIXED_PRICE|AUCTION}"
        params = {"q": q[:100], "limit": str(limit), "sort": "price", "filter": filt}
        for attempt in range(3):
            headers = {"Authorization": f"Bearer {await self.token()}", "X-EBAY-C-MARKETPLACE-ID": market,
                       "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3D{dest}"}
            async with self._sem:
                r = await self._http.get(f"{self.base}/buy/browse/v1/item_summary/search", params=params,
                                         headers=headers)
            if r.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"{market}: HTTP {r.status_code} {r.text[:160]}")
            items = [i for i in (EbayItem.from_api(market, d) for d in r.json().get("itemSummaries") or []) if i]
            for i in items:
                i.dest = dest
            self._cache[key] = (time.time(), items)
            return items
        return []


def _usd(v: float | None, cur: str) -> float | None:
    from . import models
    if v is None:
        return None
    if cur == "USD":
        return v
    return models.FX.to_usd(v, cur) if models.FX else None


async def fill_destinations(client: "EbayClient", matches: list[Match], dests: list[str]) -> None:
    """Ask eBay, per listing, what shipping costs to each of your addresses that was not searched directly."""
    async def one(m: Match, d: str) -> None:
        q = await client.ship_quote(m.item.item_id, m.item.market, d)
        if q is None:
            m.options[d] = None
        else:
            cost, cur = q
            s = _usd(cost, cur)
            m.options[d] = (m.price_usd + s) if (m.price_usd is not None and s is not None) else None
    await asyncio.gather(*[one(m, d) for m in matches for d in dests if d not in m.options])
    for m in matches:
        avail = {d: v for d, v in m.options.items() if v is not None}
        if avail:
            m.dest = min(avail, key=avail.get)
            m.total_usd = avail[m.dest]
        m.alts = {d: v for d, v in avail.items() if d != m.dest}


async def find(client: EbayClient, user_query: str, *, tr=None, markets: list[str] | None = None,
               auctions: bool = False, progress=None, dests: list[str] | None = None,
               europe: str = "DE") -> SearchOutcome:
    """Search every eBay site in its own language, for every destination you can receive at (UAE / USA /
    Europe); keep what matches; rank each listing by its cheapest delivered price (USD)."""
    from . import models
    intent = EQ.parse(user_query)
    markets = markets or list(MARKETS)
    dests = dests or [client.dest]
    langs = sorted({MARKETS[m][0] for m in markets})
    if tr and intent.words:                       # leftover words (player names, special terms) per language
        for w in intent.words:
            intent.words_tr[w] = {}
            for lang in langs:
                if lang != "en":
                    try:
                        intent.words_tr[w][lang] = (await tr.to_language(w, lang)).lower()
                    except Exception:            # noqa: BLE001
                        pass
    queries = {m: EQ.build_queries(intent, MARKETS[m][0]) for m in markets}
    plan = [(m, d) for m in markets for d in dests_for(m, dests, europe)]
    errors: list[str] = []
    done = 0

    async def one(m: str, d: str) -> list[EbayItem]:
        nonlocal done
        got: list[EbayItem] = []
        for q in queries[m]:
            try:
                got += await client.search(m, q, auctions=auctions, dest=d)
            except EbayNotConfigured:
                raise
            except Exception as e:                # noqa: BLE001
                log.warning("eBay %s->%s %r: %s", m, d, q, e)
                errors.append(m)
        done += 1
        if progress:
            await progress(done, len(plan))
        return got

    lists = await asyncio.gather(*[one(m, d) for m, d in plan])
    per_dest: dict[tuple[str, str], Match] = {}
    total = 0

    def usd(v: float | None, cur: str) -> float | None:
        if v is None:
            return None
        if cur == "USD":
            return v
        return models.FX.to_usd(v, cur) if models.FX else None

    # eBay machine-translates titles per site, so one listing can appear under several titles. If ANY version
    # is excluded (empty, other collection, singles, ...), the listing is dropped everywhere.
    verdicts: dict[tuple[str, str], EQ.Verdict] = {}
    excluded: set[str] = set()
    for items in lists:
        for it in items:
            v = verdicts.setdefault((it.legacy_id, it.title), EQ.judge(intent, it.title))
            if not v.ok and not v.reason.startswith(("missing", "not ")):
                excluded.add(it.legacy_id)
    for items in lists:
        for it in items:
            total += 1
            v = verdicts[(it.legacy_id, it.title)]
            if not v.ok or it.legacy_id in excluded:
                continue
            p_usd, s_usd = usd(it.price, it.currency), usd(it.ship, it.ship_currency)
            tot = (p_usd + s_usd) if (p_usd is not None and s_usd is not None) else None
            m = Match(it, p_usd, s_usd, tot, v.score, v.missing_soft, note=v.note, dest=it.dest)
            key = (it.legacy_id, it.dest)
            prev = per_dest.get(key)
            if prev is None:
                per_dest[key] = m
            else:                      # same listing seen on several eBay sites: keep the seller's own site
                prev_home = prev.item.market == prev.item.home_market
                this_home = it.market == it.home_market
                if (this_home and not prev_home) or (this_home == prev_home and (tot or 1e12) < (prev.total_usd or 1e12)):
                    per_dest[key] = m
    # listings without a shipping quote: estimate with the median shipping of the others (flagged in the UI)
    known = sorted(m.ship_usd for m in per_dest.values() if m.ship_usd is not None)
    median = known[len(known) // 2] if known else 0.0
    for m in per_dest.values():
        if m.total_usd is None and m.price_usd is not None:
            m.total_usd = m.price_usd + median
    best: dict[str, Match] = {}
    options: dict[str, dict] = {}
    for (lid, d), m in per_dest.items():
        options.setdefault(lid, {})[d] = m.total_usd
        if lid not in best or (m.total_usd or 1e12) < (best[lid].total_usd or 1e12):
            best[lid] = m
    matches = list(best.values())
    for m in matches:
        m.options = dict(options[m.item.legacy_id])
        m.alts = {d: t for d, t in m.options.items() if d != m.dest and t is not None}
    key = lambda m: (bool(m.missing_soft), m.total_usd if m.total_usd is not None else 1e12, -m.score)
    matches.sort(key=key)
    # the best candidates: get the real price to EVERY one of your addresses, then rank again
    await fill_destinations(client, matches[:15], dests)
    matches.sort(key=key)
    return SearchOutcome(intent, queries, total, matches, sorted(set(errors)), plan)

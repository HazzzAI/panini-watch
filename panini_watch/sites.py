"""Shop definitions, per-platform adapters (Magento / Shopify), search and category discovery."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import yaml

from . import football
from .browser import Fetcher
from .models import Listing, Product
from .parsers import parse_nav_links
from .store import Store

log = logging.getLogger("panini.sites")
DEFAULT_ISO = {"es": "EUR", "it": "EUR", "fr": "EUR", "de": "EUR", "uk": "GBP", "pt": "EUR", "nl": "EUR",
               "be": "EUR", "ch": "CHF", "pl": "PLN", "ro": "RON", "hu": "HUF", "gr": "EUR", "dk": "DKK",
               "no": "NOK", "se": "SEK", "fi": "EUR", "il": "ILS", "int": "EUR", "hobby": "EUR", "br": "BRL",
               "mx": "MXN", "cl": "CLP", "uy": "UYU", "ar": "ARS", "co": "COP"}
SKIP_PRODUCT: list[str] = []   # filled by load_config()

# Menu entries that are "new arrivals / upcoming" pages in the shops' languages.
_NEW_RE = re.compile(
    r"\b(new|novedad(es)?|novita|novit[aà]|nouveaut[eé]s?|neuheiten|neu|nieuw|nieuwe|nyhet(er)?|nyheder|uutuudet?|"
    r"nowo[sś]ci|novidades?|lan[cç]amentos?|coming[- ]soon|preventas?|pre-?orders?|pre-?vendas?|precommande|"
    r"vorbestellung|voorbestellen|prenota|kommer|upcoming|releases?|lanzamientos?|proximamente|"
    r"ofertas?|offers?|deals?|sale|saldao|promo(cion(es)?)?|bundles?)\b",
    re.I,
)
_NEW_WORDS = ("novedades", "novita", "nouveautes", "neuheiten", "nieuw", "novidades", "lancamentos", "coming-soon",
              "preventa", "pre-order", "preorder", "new-arrivals", "new-in", "whats-new", "upcoming", "lanzamientos",
              "novit", "nyheter", "nyheder", "uutuu", "nowosci")


@dataclass
class Site:
    key: str
    name: str
    flag: str
    base: str
    locale: str
    currency: str = ""
    kind: str = "magento"
    categories: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    enabled_default: bool = True
    iso: str = ""

    @property
    def english(self) -> bool:
        return self.locale.lower().startswith("en")

    @property
    def lang(self) -> str:
        code = self.locale.split("-")[0].lower()
        return {"nb": "no", "he": "iw"}.get(code, code)

    @property
    def label(self) -> str:
        return f"{self.flag} {self.name}"

    def abs(self, url: str) -> str:
        return urljoin(self.base, url)

    def search_url(self, term: str) -> str:
        return urljoin(self.base, "catalogsearch/result/?q=" + quote(term))


@dataclass
class Config:
    raw: dict
    sites: dict[str, Site]
    interval_min: int
    page_delay: tuple[float, float]
    parallel: int
    discovery_days: int
    alerts: dict
    digest_threshold: int
    skip_words: list[str]
    skip_product: list[str] = field(default_factory=list)
    flood_limit: int = 40

    def enabled_sites(self, store: Store) -> list[Site]:
        return [s for s in self.sites.values() if site_enabled(store, s)]


def ships_here(store: Store, site: Site) -> bool | None:
    """True/False from the last UAE shipping test, None if the shop has not been tested yet."""
    ship = store.get_json(f"ship.{site.key}")
    if not ship:
        return None
    return ship.get("ships") is True


def site_enabled(store: Store, site: Site) -> bool:
    if ships_here(store, site) is False:              # does not ship to your country (or could not be verified)
        return False
    v = store.get(f"site.{site.key}.enabled")
    return site.enabled_default if v is None else v == "1"


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    skip_words = [w.lower() for w in raw.get("skip_category_words", [])]
    sites: dict[str, Site] = {}
    for key, d in raw["sites"].items():
        if not isinstance(key, str):   # YAML turns bare no/on/off/yes into booleans: quote such keys
            raise ValueError(f"site key {key!r} in config.yaml must be quoted, e.g. \"no\":")
        sites[key] = Site(
            key=key, name=d["name"], flag=d.get("flag", ""), base=d["base"], locale=d.get("locale", "en-GB"),
            currency=d.get("currency", ""), kind=d.get("kind", "magento"),
            categories=d.get("categories", []), searches=d.get("searches", []),
            skip=[w.lower() for w in d.get("skip_categories", [])] + skip_words,
            enabled_default=d.get("enabled", True), iso=d.get("iso", DEFAULT_ISO.get(key, "")),
        )
    SKIP_PRODUCT[:] = [w.lower() for w in raw.get("skip_product_words", [])]
    from .models import COMMA_SITES, SITE_ISO
    SITE_ISO.clear()
    SITE_ISO.update({k: s.iso for k, s in sites.items()})
    COMMA_SITES.clear()
    COMMA_SITES.update(k for k, d in raw["sites"].items()
                       if d.get("locale", "en-GB").split("-")[0] in
                       {"es", "it", "fr", "de", "pt", "nl", "pl", "ro", "hu", "el", "da", "nb", "sv", "fi", "cs", "tr"})
    delay = raw.get("page_delay_seconds", [1.0, 2.5])
    return Config(
        raw=raw, sites=sites, interval_min=int(raw.get("check_interval_minutes", 20)),
        page_delay=(float(delay[0]), float(delay[1])), parallel=int(raw.get("sites_in_parallel", 3)),
        discovery_days=int(raw.get("discovery_refresh_days", 7)),
        alerts=raw.get("alerts", {}), digest_threshold=int(raw.get("digest_threshold", 6)),
        skip_words=skip_words,
        skip_product=[w.lower() for w in raw.get("skip_product_words", [])],
        flood_limit=int(raw.get("flood_limit", 40)),
    )


# ------------------------------------------------------------------------------------------------
# Product filtering
# ------------------------------------------------------------------------------------------------
def keep_product(p: Product, mode: str, extra: list[str] | None = None, skip: list[str] | None = None,
                 extra_text: str = "") -> bool:
    """strict: must look like football. loose: not another sport/franchise. none: keep all."""
    if mode == "none":
        return True
    if skip and any(w in football._norm(p.name) for w in skip):
        return False
    text = f"{p.name} {p.meta} {extra_text}"
    if mode == "loose":
        return not football.is_excluded(text)
    return football.is_football(text) or bool(extra and football.matches_keywords(text, extra))


# ------------------------------------------------------------------------------------------------
# Fetching (platform adapters)
# ------------------------------------------------------------------------------------------------
async def fetch_source(f: Fetcher, site: Site, kind: str, value: str, *, max_pages: int = 12) -> Listing:
    if site.kind == "shopify":
        return await _shopify_source(f, site, kind, value)
    url = site.search_url(value) if kind == "search" else site.abs(value)
    return await f.listing(site.key, site.locale, url, max_pages=max_pages)


async def live_search(f: Fetcher, site: Site, term: str) -> Listing:
    return await fetch_source(f, site, "search", term, max_pages=2)


def _shopify_product(site: Site, d: dict) -> Product:
    variants = d.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price") not in (None, "")]
    compare = [float(v["compare_at_price"]) for v in variants if v.get("compare_at_price")]
    price = min(prices) if prices else None
    old = max(compare) if compare and price is not None and max(compare) > price else None
    avail = any(v.get("available") for v in variants) if variants else bool(d.get("available"))
    imgs = d.get("images") or []
    image = (imgs[0].get("src") if imgs else "") or ""
    if not image and isinstance(d.get("image"), str):
        image = d["image"]
    if image.startswith("//"):
        image = "https:" + image
    handle = d.get("handle", "")
    url = urljoin(site.base, f"products/{handle}") if handle else site.base
    meta = " ".join([d.get("product_type", "") or ""] + [t for t in (d.get("tags") or []) if isinstance(t, str)])
    return Product(site=site.key, pid=str(d["id"]), name=d.get("title", ""), url=url, price=price, old_price=old,
                   currency=site.currency, in_stock=avail, image=image,
                   sku=(variants[0].get("sku") if variants else "") or "", meta=meta)


async def _shopify_source(f: Fetcher, site: Site, kind: str, value: str) -> Listing:
    out: list[Product] = []
    seen: set[str] = set()
    err = ""
    complete = True
    try:
        if kind == "search":
            data = await f.json(site.key, site.locale, urljoin(
                site.base, f"search/suggest.json?q={quote(value)}&resources[type]=product&resources[limit]=10"))
            items = (((data.get("resources") or {}).get("results") or {}).get("products")) or []
            for d in items:
                handle = d.get("handle") or (d.get("url", "").split("/products/")[-1].split("?")[0])
                prod = Product(site=site.key, pid=str(d.get("id")), name=d.get("title", ""),
                               url=urljoin(site.base, d.get("url", f"products/{handle}")),
                               price=float(d["price"]) if d.get("price") else None,
                               currency=site.currency, in_stock=bool(d.get("available", True)),
                               image=("https:" + d["image"]) if str(d.get("image", "")).startswith("//") else d.get("image", ""))
                if prod.pid not in seen:
                    seen.add(prod.pid); out.append(prod)
        else:
            base = value if value.startswith("http") else urljoin(site.base, value)
            pages = 2 if kind == "feed" else 6
            for page in range(1, pages + 1):
                sep = "&" if "?" in base else "?"
                data = await f.json(site.key, site.locale, f"{base}{sep}limit=250&page={page}")
                items = data.get("products") or []
                for d in items:
                    prod = _shopify_product(site, d)
                    if prod.pid not in seen:
                        seen.add(prod.pid); out.append(prod)
                if len(items) < 250:
                    break
    except Exception as e:  # network / JSON problems
        err, complete = str(e)[:200], False
    return Listing(value, out, None, complete, err)


# ------------------------------------------------------------------------------------------------
# Discovery
# ------------------------------------------------------------------------------------------------
def _is_skipped(site: Site, text: str) -> bool:
    t = football._norm(text)
    return any(w in t for w in site.skip)


def _looks_new(text: str, url: str) -> bool:
    t = football._norm(text)
    slug = urlparse(url).path.lower()
    return bool(_NEW_RE.search(t)) or any(w in slug for w in _NEW_WORDS)


async def discover(f: Fetcher, site: Site, store: Store, *, log_fn=None) -> dict:
    """Find the football categories (and 'new arrivals' pages) of a shop and store them as sources.

    Magento shops: read the menu, scan the football-looking categories once and keep the smallest set
    that covers every product (so parents that already include their children replace them).
    """
    say = log_fn or (lambda *_: None)
    had_baseline = {(r["kind"], r["value"]) for r in store.sources(site.key, enabled_only=False) if r["baselined"]}
    store.delete_sources(site.key, "auto")
    added = {"categories": 0, "feeds": 0, "searches": 0}

    # user/config supplied categories and searches
    for c in site.categories:
        store.add_source(site.key, "category", site.abs(c), c, "loose", "config")
    for term in site.searches:
        store.add_source(site.key, "search", term, term, "strict", "config")

    if site.kind == "shopify":
        data = await f.json(site.key, site.locale, urljoin(site.base, "collections.json?limit=250"))
        for c in data.get("collections", []):
            title = f"{c.get('title', '')} {c.get('handle', '')}"
            if c.get("products_count", 0) and not _is_skipped(site, title):
                if football.is_football(title):
                    store.add_source(site.key, "category", urljoin(site.base, f"collections/{c['handle']}/products.json"),
                                     c.get("title", c["handle"]), "loose")
                    added["categories"] += 1
        store.add_source(site.key, "feed", urljoin(site.base, "products.json"), "Newest products", "strict")
        added["feeds"] += 1
        store.set(f"discovered.{site.key}", str(__import__("time").time()))
        _restore_baselines(store, site, had_baseline)
        return added

    html = await f.html(site.key, site.locale, site.base, "nav, .navigation")
    nav = parse_nav_links(html, site.base)
    fb: dict[str, str] = {}
    news: dict[str, str] = {}
    for text, url in nav:
        slug = url.replace(site.base, "")
        label = f"{text} {slug}"
        if _is_skipped(site, label):
            continue
        if football.is_football(label):
            fb[url] = text or slug
        elif _looks_new(text, url) and not football.is_excluded(label):
            news[url] = text or slug
    say(f"{site.key}: {len(nav)} menu links, {len(fb)} football, {len(news)} new/offer pages")

    # scan football candidates, keep a covering set
    scanned: list[tuple[str, str, set[str]]] = []
    for url, name in list(fb.items())[:80]:
        L = await f.listing(site.key, site.locale, url, max_pages=6)
        pids = {p.pid for p in L.products if keep_product(p, "loose", skip=SKIP_PRODUCT)}
        if pids:
            scanned.append((url, name, pids))
    scanned.sort(key=lambda t: -len(t[2]))
    covered: set[str] = set()
    for url, name, pids in scanned:
        if pids - covered:
            store.add_source(site.key, "category", url, name, "loose")
            covered |= pids
            added["categories"] += 1
    for url, name in news.items():
        if store.add_source(site.key, "category", url, name, "strict"):
            added["feeds"] += 1

    if not added["categories"] and not site.searches:
        for term in ("fifa", "world cup", "football", "futbol", "calcio", "fussball", "futebol"):
            if store.add_source(site.key, "search", term, term, "strict"):
                added["searches"] += 1
    store.set(f"discovered.{site.key}", str(__import__("time").time()))
    _restore_baselines(store, site, had_baseline)
    return added


def _restore_baselines(store: Store, site: Site, had: set) -> None:
    for r in store.sources(site.key, enabled_only=False):
        if (r["kind"], r["value"]) in had:
            store.update_source(r["id"], baselined=1)

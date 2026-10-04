"""The watch loop: scan every enabled shop, diff against what we know, emit events."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import football
from .browser import Fetcher
from .models import Product
from .sites import Config, Site, discover, fetch_source, keep_product, site_enabled
from .store import Store

log = logging.getLogger("panini.monitor")


@dataclass
class Event:
    type: str            # new | restock | discount | price_drop | sold_out
    site: Site
    product: Product
    category: str = ""
    old_price: float | None = None


Notify = Callable[[Site, list[Event]], Awaitable[bool]]


class Monitor:
    def __init__(self, cfg: Config, store: Store, fetcher: Fetcher, notify: Notify):
        self.cfg, self.store, self.fetcher, self.notify = cfg, store, fetcher, notify
        self.wake = asyncio.Event()
        self.scanning: set[str] = set()
        self.last_cycle: float | None = None
        self.on_cycle: Callable[[list[dict]], Awaitable[None]] | None = None
        self.tr = None            # i18n.Translator (set by the app); product names become English
        self.notify_batch = None      # async (events across all shops) -> bool ; groups same product across shops
        self._batch: dict[str, list[Event]] = {}
        self.allow_discovery = True   # False in short scheduled runs (a weekly job re-discovers instead)
        self._extra_kw = [k for k in cfg.raw.get("extra_football_keywords", []) if k]

    # -- helpers ---------------------------------------------------------------------------
    def alert_on(self, kind: str) -> bool:
        override = self.store.get(f"alert.{kind}")
        if override is not None:
            return override == "1"
        return bool(self.cfg.alerts.get(kind if kind != "new" else "new_product", kind == "new"))

    def muted(self) -> bool:
        return self.store.get("muted") == "1"

    async def ensure_discovery(self, site: Site, force: bool = False) -> None:
        last = float(self.store.get(f"discovered.{site.key}", "0") or 0)
        stale = (time.time() - last) > self.cfg.discovery_days * 86400 and self.allow_discovery
        if force or stale or not self.store.sources(site.key, enabled_only=False):
            log.info("[%s] discovering football categories…", site.key)
            added = await discover(self.fetcher, site, self.store, log_fn=log.info)
            log.info("[%s] discovery done: %s", site.key, added)

    # -- scanning ----------------------------------------------------------------------------
    async def scan_site(self, site: Site, *, force_discovery: bool = False) -> dict:
        if site.key in self.scanning:
            return {"site": site.key, "skipped": True}
        self.scanning.add(site.key)
        t0 = time.time()
        summary = {"site": site.key, "products": 0, "events": 0, "errors": 0, "sources": 0}
        try:
            try:
                await self.ensure_discovery(site, force_discovery)
            except Exception as e:
                log.warning("[%s] discovery failed: %s", site.key, e)
                if not self.store.sources(site.key):
                    summary["errors"] += 1
                    return summary

            found: dict[str, tuple[Product, str, bool]] = {}
            exempt: set[str] = set()          # products from your own /watch keywords are never filtered
            complete_sources: list[int] = []
            for src in self.store.sources(site.key):
                summary["sources"] += 1
                src_name = src["name"]
                if self.tr and not site.english and src["kind"] != "search":
                    src_name = await self.tr.one(src_name, source_lang=site.lang)
                try:
                    L = await fetch_source(self.fetcher, site, src["kind"], src["value"])
                except Exception as e:
                    L = None
                    err = str(e)[:200]
                else:
                    err = L.error
                self.store.update_source(src["id"], last_scan=time.time(),
                                         last_count=len(L.products) if L else 0, last_error=err or "")
                if L is None or (err and not L.products):
                    summary["errors"] += 1
                    log.warning("[%s] source %s failed: %s", site.key, src["name"], err)
                    continue
                silent = not src["baselined"]
                for p in L.products:
                    if not keep_product(p, src["mode"], self._extra_kw, self.cfg.skip_product):
                        continue
                    if src["mode"] == "none":
                        exempt.add(p.pid)
                    prev = found.get(p.pid)
                    found[p.pid] = (p, src_name, (prev[2] and silent) if prev else silent)
                if L.complete and not src["baselined"]:
                    complete_sources.append(src["id"])

            if self.tr and not site.english and found:
                plist = [v[0] for v in found.values()]
                english = await self.tr.many([p.name for p in plist], source_lang=site.lang, strict=True)
                known = self.store.products_for_site(site.key)
                for p, en in zip(plist, english):
                    if en is not None:
                        p.orig_name, p.name = p.name, en
                    elif p.pid in known and known[p.pid]["name_orig"] is not None:
                        p.name = known[p.pid]["name"]     # translation busy: keep the English name we have
                    else:                                      # new product while translators are paused
                        p.name = self.tr.rough(p.name)       # glossary English now; retried next cycle
            if found and self.cfg.skip_product:       # single "missing card" listings, also after translation
                for pid in [k for k, (p, _, _) in found.items() if k not in exempt and
                            any(w in football._norm(p.name) for w in self.cfg.skip_product)]:
                    del found[pid]
            events = self._diff(site, found)
            self.store.upsert_products((p, name, silent) for p, name, silent in found.values())
            for sid in complete_sources:
                self.store.update_source(sid, baselined=1)
            summary["products"] = len(found)
            summary["events"] = len(events)
            for e in events:
                self.store.log_event(site.key, e.product.pid, e.type, e.product.name)
            if events and not self.muted():
                events = [e for e in events if self.alert_on(e.type)]
                if events and self.notify_batch:
                    self._batch[site.key] = events          # delivered together after all shops are done
                elif events:
                    ok = await self.notify(site, events)
                    if not ok:
                        self._queue_pending(site, events)
            return summary
        finally:
            self.scanning.discard(site.key)
            log.info("[%s] scan done in %.0fs: %s", site.key, time.time() - t0, summary)

    def _diff(self, site: Site, found: dict[str, tuple[Product, str, bool]]) -> list[Event]:
        known = self.store.products_for_site(site.key)
        min_pct = float(self.cfg.alerts.get("price_drop_min_pct", 10))
        min_disc = float(self.cfg.alerts.get("discount_min_pct", 10))
        events: list[Event] = []
        for pid, (p, cat, silent) in found.items():
            row = known.get(pid)
            if row is None:
                if not silent:
                    events.append(Event("new", site, p, cat))
                continue
            was_in = bool(row["in_stock"])
            if p.in_stock and not was_in:
                events.append(Event("restock", site, p, cat))
            elif was_in and not p.in_stock:
                events.append(Event("sold_out", site, p, cat))
            old = row["price"]
            disc = p.discount_pct
            prev_disc = None
            if row["old_price"] and old and row["old_price"] > old:
                prev_disc = round((1 - old / row["old_price"]) * 100)
            if disc and disc >= min_disc and (prev_disc is None or disc > prev_disc):
                events.append(Event("discount", site, p, cat, old_price=p.old_price))
            elif p.price and old and p.price < old and (1 - p.price / old) * 100 >= min_pct:
                events.append(Event("price_drop", site, p, cat, old_price=old))
        return events

    # -- retry queue for failed deliveries ---------------------------------------------------
    def _queue_pending(self, site: Site, events: list[Event]) -> None:
        pending = self.store.get_json("pending", []) or []
        for e in events:
            pending.append({"site": site.key, "type": e.type, "pid": e.product.pid, "cat": e.category,
                            "old": e.old_price})
        self.store.set_json("pending", pending[-300:])
        log.warning("%d alerts queued for retry", len(events))

    async def flush_pending(self) -> None:
        pending = self.store.get_json("pending", []) or []
        if not pending:
            return
        by_site: dict[str, list[Event]] = {}
        for d in pending:
            site = self.cfg.sites.get(d["site"])
            row = self.store.product(d["site"], d["pid"])
            if not site or not row:
                continue
            import json
            p = Product(site=d["site"], pid=d["pid"], name=row["name"], url=row["url"], price=row["price"],
                        old_price=row["old_price"], currency=row["currency"] or "", in_stock=bool(row["in_stock"]),
                        image=row["image"] or "", labels=json.loads(row["labels"] or "[]"))
            by_site.setdefault(d["site"], []).append(Event(d["type"], site, p, d.get("cat", ""), d.get("old")))
        self.store.set_json("pending", [])
        for key, evs in by_site.items():
            if not await self.notify(self.cfg.sites[key], evs):
                self._queue_pending(self.cfg.sites[key], evs)

    async def scan_all(self, only: list[str] | None = None, *, force_discovery: bool = False) -> list[dict]:
        sites = [s for s in self.cfg.enabled_sites(self.store) if not only or s.key in only]
        results = await asyncio.gather(*[self.scan_site(s, force_discovery=force_discovery) for s in sites],
                                       return_exceptions=True)
        out = []
        for s, r in zip(sites, results):
            if isinstance(r, Exception):
                log.exception("[%s] scan crashed", s.key, exc_info=r)
                out.append({"site": s.key, "errors": 1, "crash": str(r)})
            else:
                out.append(r)
        self.last_cycle = time.time()
        self.store.set("last_cycle", str(self.last_cycle))
        if self.notify_batch and self._batch:
            batch, self._batch = self._batch, {}
            allev = [e for evs in batch.values() for e in evs]
            if not await self.notify_batch(allev):
                for key, evs in batch.items():
                    self._queue_pending(self.cfg.sites[key], evs)
        return out

    async def run_forever(self) -> None:
        while True:
            t0 = time.time()
            try:
                await self.flush_pending()
                results = await self.scan_all()
                if self.on_cycle:
                    await self.on_cycle(results)
            except Exception:
                log.exception("scan cycle failed")
            wait = max(60.0, self.cfg.interval_min * 60 - (time.time() - t0))
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

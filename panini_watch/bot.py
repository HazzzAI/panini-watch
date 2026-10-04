"""Telegram front-end: alert delivery + interactive commands (search, browse, new, sites, ...)."""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from . import football, render
from .browser import Fetcher
from .models import Product
from .monitor import Event, Monitor
from .sites import Config, Site, fetch_source, keep_product, live_search, site_enabled
from .store import Store
from .telegram import Telegram, TelegramError, button, esc, esc_attr, keyboard

log = logging.getLogger("panini.bot")

COMMANDS = [
    ("search", "Search every shop live (or just type what you want)"),
    ("browse", "Navigate shops → categories → products"),
    ("new", "Newest products we spotted (e.g. /new es 3)"),
    ("deals", "Products on discount right now (e.g. /deals es)"),
    ("stock", "Search, only what is in stock"),
    ("watch", "Watch a keyword on all shops (e.g. /watch Megacracks)"),
    ("watchlist", "Show / remove your keyword watches"),
    ("sites", "Turn shops on or off"),
    ("alerts", "Choose which alerts you get"),
    ("status", "Health of the watcher"),
    ("scan", "Check the shops right now"),
    ("mute", "Pause alerts"),
    ("unmute", "Resume alerts"),
    ("help", "How to use this bot"),
]

HELP = (
    "<b>⚽ Panini Watch</b>\n"
    "I watch every official Panini shop and ping you when a <b>football</b> product drops, comes back in stock "
    "or gets cheaper.\n\n"
    "<b>Just type</b> what you want, e.g. <code>world cup</code> or <code>megacracks @es</code> — I search the "
    "shops live and show what is ✅ in stock / ❌ sold out.\n"
    "Add <code>@es @it @uk</code> to limit the search to certain shops.\n\n"
    "<b>Commands</b>\n"
    "/search <i>words</i> · /stock <i>words</i> (in stock only)\n"
    "/browse — shops → categories → products\n"
    "/deals [shop] — everything currently on discount\n"
    "/new [shop] [days] — newest finds (<code>/new es 3</code>)\n"
    "/watch <i>words</i> [@shop] — alert me for this keyword · /watchlist\n"
    "/sites — enable / disable shops · /alerts — pick alert types\n"
    "/status · /scan [shop] · /mute · /unmute\n\n"
    "Send me any Panini category link and I will start watching it."
)


@dataclass
class ResultSession:
    id: str
    title: str
    rows: list[tuple[Site, Product]]
    page: int = 0
    only_stock: bool = False
    site: str | None = None          # active shop filter
    back: str | None = None          # callback data for a Back button
    per_page: int = 8
    notes: str = ""

    def view(self) -> list[tuple[Site, Product]]:
        rows = self.rows
        if self.site:
            rows = [r for r in rows if r[0].key == self.site]
        if self.only_stock:
            rows = [r for r in rows if r[1].in_stock]
        return rows


class Bot:
    def __init__(self, tg: Telegram, cfg: Config, store: Store, fetcher: Fetcher, monitor: Monitor):
        self.tg, self.cfg, self.store, self.fetcher, self.monitor = tg, cfg, store, fetcher, monitor
        self.sessions: OrderedDict[str, ResultSession] = OrderedDict()
        self._ids = itertools.count(int(time.time()) % 100000)
        self.awaiting: str | None = None     # next plain-text message is a "search" / "watch" term
        self._offset: int | None = None
        self.monitor.notify = self.notify
        self.monitor.notify_batch = self.notify_batch
        self.tr = None        # i18n.Translator (set by the app)

    # ------------------------------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------------------------------
    def _authorised(self, chat_id) -> bool:
        if not self.tg.chat_id:
            return True
        return str(chat_id) == str(self.tg.chat_id)

    def _bind_chat(self, chat_id) -> None:
        if not self.tg.chat_id:
            self.tg.chat_id = str(chat_id)
            self.store.set("chat_id", str(chat_id))
            log.info("bound to chat %s", chat_id)

    # ------------------------------------------------------------------------------------------
    # alert delivery (called by the monitor)
    # ------------------------------------------------------------------------------------------
    async def notify(self, site: Site, events: list[Event]) -> bool:
        if not self.tg.chat_id:
            log.warning("no chat bound yet, cannot deliver %d alerts", len(events))
            return False
        try:
            for kind in ("new", "restock", "discount", "price_drop", "sold_out"):
                evs = [e for e in events if e.type == kind]
                if not evs:
                    continue
                if len(evs) > self.cfg.digest_threshold:
                    shown = [e.product for e in evs[:60]]
                    for text, markup in render.digest(kind, site, shown):
                        await self.tg.send(text, markup=markup)
                    if len(evs) > 60:
                        await self.tg.send(f"…and {len(evs) - 60} more on {esc(site.label)}. Use /new {site.key}")
                    continue
                for e in evs:
                    extra = ""
                    if kind == "price_drop" and e.old_price:
                        extra = f"was {esc(Product(site.key, '', '', '', price=e.old_price, currency=e.product.currency).price_text())}"
                    text, markup = render.product_card(kind, site, e.product, extra=extra, category=e.category)
                    if e.product.image:
                        await self.tg.send_photo(e.product.image, text, markup=markup)
                    else:
                        await self.tg.send(text, markup=markup)
                    await asyncio.sleep(0.35)
            return True
        except TelegramError as e:
            log.error("alert delivery failed: %s", e)
            return False

    async def notify_batch(self, events: list[Event]) -> bool:
        """All alerts of one check cycle, across every shop: same product in several shops = one message,
        many at once = a digest, a flood (catalogue reshuffle) = a single summary."""
        if not self.tg.chat_id:
            log.warning("no chat bound yet, cannot deliver %d alerts", len(events))
            return False
        order = {k: i for i, k in enumerate(self.cfg.sites)}
        try:
            for kind in ("new", "discount", "restock", "price_drop", "sold_out"):
                evs = [e for e in events if e.type == kind]
                if not evs:
                    continue
                groups: dict[str, list[Event]] = {}
                for e in evs:
                    groups.setdefault(render.group_key(e.product), []).append(e)
                glist = [sorted(g, key=lambda e: order.get(e.site.key, 99)) for g in groups.values()]
                glist.sort(key=lambda g: -len(g))
                if len(glist) > self.cfg.flood_limit:
                    icon, label = render._HEAD[kind]
                    head = (f"{icon} <b>{len(glist)} {label.lower()} alerts appeared at once</b> — probably a shop "
                            f"re-organising its catalogue, so I am not sending them one by one.\nA few of them:")
                    lines = [head, ""] + [
                        f'{render.STOCK_ICON[g[0].product.in_stock]} <a href="{esc_attr(g[0].product.url)}">'
                        f'{esc(g[0].product.name[:70])}</a> {"".join(e.site.flag for e in g)}' for g in glist[:8]]
                    lines.append("\nBrowse them with /new or /deals.")
                    await self.tg.send("\n".join(lines))
                elif len(glist) > self.cfg.digest_threshold:
                    for text in render.group_digest(kind, glist):
                        await self.tg.send(text)
                else:
                    for g in glist:
                        text, markup = render.group_card(kind, g)
                        photo = next((e.product.image for e in g if e.product.image), "")
                        if photo and len(text) <= 1000:
                            await self.tg.send_photo(photo, text, markup=markup)
                        else:
                            await self.tg.send(text, markup=markup)
                        await asyncio.sleep(0.35)
            return True
        except TelegramError as e:
            log.error("alert delivery failed: %s", e)
            return False

    # ------------------------------------------------------------------------------------------
    # polling loop
    # ------------------------------------------------------------------------------------------
    async def run(self, deadline: float | None = None) -> None:
        """Poll Telegram forever, or until `deadline` (epoch seconds) in scheduled cloud runs."""
        saved = self.store.get("tg_offset")
        if saved and self._offset is None:
            self._offset = int(saved)
        try:
            await self.tg.set_commands(COMMANDS)
        except TelegramError as e:
            log.warning("could not set command menu: %s", e)
        tasks: set[asyncio.Task] = set()
        while True:
            wait = 50
            if deadline is not None:
                wait = min(50, int(deadline - time.time()))
                if wait <= 0:
                    break
            try:
                updates = await self.tg.updates(self._offset, timeout=wait)
            except TelegramError as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self._offset = u["update_id"] + 1
                t = asyncio.create_task(self._safe_handle(u))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            if updates:
                self.store.set("tg_offset", str(self._offset))
        if tasks:                                   # let in-flight commands finish
            await asyncio.wait(tasks, timeout=60)

    async def _safe_handle(self, u: dict) -> None:
        try:
            if "message" in u:
                await self.on_message(u["message"])
            elif "callback_query" in u:
                await self.on_callback(u["callback_query"])
        except Exception:
            log.exception("handler crashed")
            chat = (u.get("message") or (u.get("callback_query") or {}).get("message") or {}).get("chat", {}).get("id")
            if chat and self._authorised(chat):
                try:
                    await self.tg.send("⚠️ Something went wrong with that request. Try again.", chat_id=chat)
                except TelegramError:
                    pass

    # ------------------------------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------------------------------
    async def on_message(self, m: dict) -> None:
        chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if not text:
            return
        cmd = text.split()[0].lower() if text.startswith("/") else ""
        if not self._authorised(chat):
            log.warning("ignored message from unauthorised chat %s", chat)
            return
        if cmd.split("@")[0] == "/start":
            self._bind_chat(chat)
        elif not self.tg.chat_id:
            return
        if cmd:
            name = cmd[1:].split("@")[0]
            arg = text[len(cmd):].strip()
            handler = getattr(self, f"cmd_{name}", None)
            if handler:
                self.awaiting = None
                await handler(chat, arg)
            else:
                await self.tg.send("Unknown command. Try /help", chat_id=chat)
            return
        if self.awaiting == "watch":
            self.awaiting = None
            return await self.cmd_watch(chat, text)
        if self.awaiting == "search_stock":
            self.awaiting = None
            return await self.cmd_stock(chat, text)
        self.awaiting = None
        # a Panini link -> watch that category
        if re.match(r"https?://\S+$", text):
            return await self._add_link(chat, text)
        await self.cmd_search(chat, text)

    # ---- /help /start ------------------------------------------------------------------------
    async def cmd_start(self, chat, arg: str) -> None:
        enabled = len(self.cfg.enabled_sites(self.store))
        await self.tg.send(HELP + f"\n\n✅ Connected. Watching <b>{enabled}</b> shops.", chat_id=chat,
                           markup=self._main_menu())

    async def cmd_help(self, chat, arg: str) -> None:
        await self.tg.send(HELP, chat_id=chat, markup=self._main_menu())

    def _main_menu(self) -> dict:
        return keyboard([
            [button("🔎 Search", data="m:search"), button("🧭 Browse", data="m:browse")],
            [button("🆕 Newest finds", data="m:new"), button("🏷 Deals", data="m:deals")],
            [button("✅ In stock", data="m:stock"), button("🏪 Shops", data="m:sites")],
            [button("📊 Status", data="m:status")],
        ])

    # ---- search ------------------------------------------------------------------------------
    def _parse_scope(self, arg: str) -> tuple[str, list[Site]]:
        keys = re.findall(r"(?:^|\s)@(\w+)", arg)
        term = re.sub(r"(?:^|\s)@\w+", " ", arg).strip()
        enabled = self.cfg.enabled_sites(self.store)
        if keys:
            wanted = {k.lower() for k in keys}
            sites = [s for s in self.cfg.sites.values() if s.key in wanted]
            if not sites:
                sites = enabled
        else:
            sites = enabled
        return term, sites

    async def cmd_search(self, chat, arg: str, only_stock: bool = False) -> None:
        term, sites = self._parse_scope(arg)
        if not term:
            self.awaiting = "search_stock" if only_stock else None
            return await self.tg.send("What should I search for? e.g. <code>world cup</code> or "
                                      "<code>megacracks @es</code>", chat_id=chat)
        msg = await self.tg.send(f"🔎 Searching <b>{esc(term)}</b> on {len(sites)} shops…", chat_id=chat)
        rows: list[tuple[Site, Product]] = []
        errors: list[str] = []
        done = 0
        last_edit = 0.0

        async def one(site: Site):
            nonlocal done, last_edit
            try:
                terms = [term]
                if self.tr and not site.english:      # also search with the English words translated
                    local = await self.tr.to_language(term, site.lang)
                    if local and local.lower() != term.lower():
                        terms.append(local)
                lists = await asyncio.gather(*[live_search(self.fetcher, site, t) for t in terms])
                got, seen = [], set()
                for L in lists:
                    for p in L.products:
                        if p.pid not in seen:
                            seen.add(p.pid)
                            got.append((site, p))
                if all(L.error and not L.products for L in lists):
                    errors.append(site.key)
                return got
            except Exception as e:
                log.warning("search %s failed: %s", site.key, e)
                errors.append(site.key)
                return []
            finally:
                done += 1
                if time.time() - last_edit > 4 and done < len(sites):
                    last_edit = time.time()
                    try:
                        await self.tg.edit(chat, msg["message_id"],
                                           f"🔎 Searching <b>{esc(term)}</b>… {done}/{len(sites)} shops done")
                    except TelegramError:
                        pass

        results = await asyncio.gather(*[one(s) for s in sites])
        order = {s.key: i for i, s in enumerate(self.cfg.sites.values())}
        for r in results:
            rows.extend(r)
        # hide single "missing card/sticker" listings unless the query itself asks for them
        skip = [w for w in self.cfg.skip_product if w not in football._norm(term)]
        rows = [(s, p) for s, p in rows if not any(w in football._norm(p.name) for w in skip)]
        rows.sort(key=lambda sp: order.get(sp[0].key, 99))
        sess = self._new_session(f"Search: {term}", rows, only_stock=only_stock)
        if errors:
            sess.notes = "⚠️ Could not read: " + ", ".join(errors)
        await self._show(chat, sess, msg["message_id"])

    async def cmd_stock(self, chat, arg: str) -> None:
        await self.cmd_search(chat, arg, only_stock=True)

    # ---- new ---------------------------------------------------------------------------------
    async def cmd_new(self, chat, arg: str) -> None:
        parts = arg.split()
        site = next((p for p in parts if p in self.cfg.sites), None)
        days = next((int(p) for p in parts if p.isdigit()), 7)
        rows_db = self.store.recent_new(site, time.time() - days * 86400, limit=300)
        rows = [(self.cfg.sites[r["site"]], self._row_to_product(r)) for r in rows_db if r["site"] in self.cfg.sites]
        title = f"New finds, last {days} day(s)" + (f" — {self.cfg.sites[site].label}" if site else "")
        sess = self._new_session(title, rows)
        if not rows:
            return await self.tg.send(
                f"Nothing new in the last {days} day(s). I only report products that appear <i>after</i> the first "
                f"scan of a shop.", chat_id=chat)
        await self._show(chat, sess)

    # ---- deals -------------------------------------------------------------------------------
    async def cmd_deals(self, chat, arg: str) -> None:
        site = next((p for p in arg.lower().split() if p in self.cfg.sites), None)
        enabled = {s.key for s in self.cfg.enabled_sites(self.store)}
        rows_db = [r for r in self.store.deals(site) if r["site"] in enabled and r["site"] in self.cfg.sites]
        rows = [(self.cfg.sites[r["site"]], self._row_to_product(r)) for r in rows_db]
        if not rows:
            return await self.tg.send("No discounted products right now (a product counts as discounted when the "
                                      "shop shows a crossed-out old price).", chat_id=chat)
        title = "Discounts right now" + (f" — {self.cfg.sites[site].label}" if site else "")
        await self._show(chat, self._new_session(title, rows, only_stock=True))

    # ---- browse ------------------------------------------------------------------------------
    async def cmd_browse(self, chat, arg: str, message_id: int | None = None) -> None:
        sites = self.cfg.enabled_sites(self.store)
        rows = []
        for i in range(0, len(sites), 3):
            rows.append([button(s.label, data=f"bs:{s.key}:0") for s in sites[i:i + 3]])
        text = "🧭 <b>Pick a shop</b>"
        if message_id:
            await self.tg.edit(chat, message_id, text, markup=keyboard(rows))
        else:
            await self.tg.send(text, chat_id=chat, markup=keyboard(rows))

    async def _browse_site(self, chat, message_id: int, key: str, page: int) -> None:
        site = self.cfg.sites[key]
        srcs = self.store.sources(key)
        if not srcs:
            await self.tg.edit(chat, message_id, f"{esc(site.label)}: no categories yet — still discovering. "
                                                 f"Try /scan {key} in a minute.",
                               markup=keyboard([[button("⬅️ Shops", data="m:browse")]]))
            return
        per = 8
        pages = max(1, (len(srcs) + per - 1) // per)
        page = min(max(page, 0), pages - 1)
        rows = []
        shown = srcs[page * per:(page + 1) * per]
        names = {s["id"]: s["name"] for s in shown}
        if self.tr and not site.english:
            en = await self.tr.many([s["name"] for s in shown if s["kind"] != "search"], source_lang=site.lang)
            it = iter(en)
            names = {s["id"]: (next(it) if s["kind"] != "search" else s["name"]) for s in shown}
        for s in shown:
            icon = "🔎" if s["kind"] == "search" else ("🆕" if s["mode"] == "strict" and s["kind"] != "search" else "📂")
            rows.append([button(f"{icon} {names[s['id']][:48]}", data=f"bc:{s['id']}")])
        nav = []
        if page > 0:
            nav.append(button("◀", data=f"bs:{key}:{page - 1}"))
        nav.append(button(f"{page + 1}/{pages}", data="noop"))
        if page < pages - 1:
            nav.append(button("▶", data=f"bs:{key}:{page + 1}"))
        rows.append(nav)
        rows.append([button("⬅️ Shops", data="m:browse")])
        await self.tg.edit(chat, message_id, f"{esc(site.label)} — <b>{len(srcs)}</b> football sections\n"
                                             f"Tap one to see its products with live stock.", markup=keyboard(rows))

    async def _browse_source(self, chat, message_id: int, sid: int) -> None:
        src = next((s for s in self.store.sources(enabled_only=False) if s["id"] == sid), None)
        if not src:
            return await self.tg.edit(chat, message_id, "That section no longer exists.")
        site = self.cfg.sites[src["site"]]
        title = src["name"]
        if self.tr and not site.english and src["kind"] != "search":
            title = await self.tr.one(title, source_lang=site.lang)
        await self.tg.edit(chat, message_id, f"⏳ Loading <b>{esc(title)}</b> from {esc(site.label)}…")
        L = await fetch_source(self.fetcher, site, src["kind"], src["value"])
        rows = [(site, p) for p in L.products if keep_product(p, src["mode"], None, self.cfg.skip_product)]
        sess = self._new_session(f"{site.flag} {title}", rows, back=f"bs:{site.key}:0")
        sess.site = None
        if L.error:
            sess.notes = f"⚠️ {esc(L.error[:120])}"
        await self._show(chat, sess, message_id, show_site=False)

    # ---- sites -------------------------------------------------------------------------------
    async def cmd_sites(self, chat, arg: str, message_id: int | None = None) -> None:
        sites = list(self.cfg.sites.values())
        rows = []
        for i in range(0, len(sites), 2):
            rows.append([button(f"{'✅' if site_enabled(self.store, s) else '⛔'} {s.label}", data=f"ts:{s.key}")
                         for s in sites[i:i + 2]])
        rows.append([button("All on", data="ts:*:1"), button("All off", data="ts:*:0")])
        text = ("🏪 <b>Shops being watched</b>\nTap to switch a shop on/off. "
                "Alerts and searches only use ✅ shops.")
        if message_id:
            await self.tg.edit(chat, message_id, text, markup=keyboard(rows))
        else:
            await self.tg.send(text, chat_id=chat, markup=keyboard(rows))

    # ---- alerts ------------------------------------------------------------------------------
    async def cmd_alerts(self, chat, arg: str, message_id: int | None = None) -> None:
        labels = [("new", "🆕 New products"), ("restock", "🔔 Back in stock"), ("discount", "🏷 Discounts / sales"), ("price_drop", "💸 Quiet price drops"),
                  ("sold_out", "🚫 Sold out")]
        rows = [[button(f"{'✅' if self.monitor.alert_on(k) else '⛔'} {lbl}", data=f"al:{k}")] for k, lbl in labels]
        muted = self.monitor.muted()
        rows.append([button("🔕 Unmute" if muted else "🔔 Mute everything", data="al:mute")])
        text = "⚙️ <b>Alert types</b>" + ("\n\n🔕 Alerts are currently <b>muted</b>." if muted else "")
        if message_id:
            await self.tg.edit(chat, message_id, text, markup=keyboard(rows))
        else:
            await self.tg.send(text, chat_id=chat, markup=keyboard(rows))

    async def cmd_mute(self, chat, arg: str) -> None:
        self.store.set("muted", "1")
        await self.tg.send("🔕 Alerts muted. /unmute to resume (I keep tracking meanwhile).", chat_id=chat)

    async def cmd_unmute(self, chat, arg: str) -> None:
        self.store.set("muted", "0")
        await self.tg.send("🔔 Alerts on again.", chat_id=chat)

    # ---- watch -------------------------------------------------------------------------------
    async def cmd_watch(self, chat, arg: str) -> None:
        term, sites = self._parse_scope(arg)
        if not term:
            self.awaiting = "watch"
            return await self.tg.send("Which keyword should I watch? e.g. <code>Megacracks</code> or "
                                      "<code>Adrenalyn @it</code>", chat_id=chat)
        n = 0
        for s in sites:
            if self.store.add_source(s.key, "search", term, term, "none", "user"):
                n += 1
        await self.tg.send(f"👀 Watching <b>{esc(term)}</b> on {len(sites)} shop(s) ({n} new). I will alert on any "
                           f"new item, restock or price drop that matches. Takes effect at the next scan "
                           f"(or /scan).", chat_id=chat)

    async def cmd_watchlist(self, chat, arg: str, message_id: int | None = None) -> None:
        srcs = [s for s in self.store.sources(enabled_only=False) if s["origin"] == "user"]
        terms: dict[str, list] = {}
        for s in srcs:
            terms.setdefault((s["kind"], s["value"], s["name"]), []).append(s)
        if not terms:
            text, markup = "You have no custom watches. Add one: /watch Megacracks", None
        else:
            rows = [[button(f"🗑 {name[:40]} ({len(v)} shop{'s' if len(v) > 1 else ''})",
                            data="uw:" + ",".join(str(x['id']) for x in v)[:55])]
                    for (_, _, name), v in terms.items()]
            text, markup = "👀 <b>Your watches</b> — tap to remove", keyboard(rows)
        if message_id:
            await self.tg.edit(chat, message_id, text, markup=markup)
        else:
            await self.tg.send(text, chat_id=chat, markup=markup)

    async def _add_link(self, chat, url: str) -> None:
        host = urlparse(url).netloc.replace("www.", "")
        site = next((s for s in self.cfg.sites.values() if urlparse(s.base).netloc.replace("www.", "") == host), None)
        if not site:
            return await self.tg.send("That is not one of the Panini shops I know. "
                                      "Use /sites to see them.", chat_id=chat)
        L = await fetch_source(self.fetcher, site, "category", url, max_pages=1)
        if not L.products:
            return await self.tg.send("I could not find products on that page.", chat_id=chat)
        title = url.rstrip("/").rsplit("/", 1)[-1].replace(".html", "").replace("-", " ").title()
        added = self.store.add_source(site.key, "category", url, title, "none", "user")
        await self.tg.send(f"📂 {'Now watching' if added else 'Already watching'} <b>{esc(title)}</b> on "
                           f"{esc(site.label)} ({len(L.products)} products right now).", chat_id=chat)

    # ---- status / scan -----------------------------------------------------------------------
    async def cmd_status(self, chat, arg: str) -> None:
        counts = self.store.counts()
        srcs = self.store.sources()
        by_site: dict[str, list] = {}
        for s in srcs:
            by_site.setdefault(s["site"], []).append(s)
        lines = ["📊 <b>Status</b>"]
        last = self.store.get("last_cycle")
        if last:
            ago = int((time.time() - float(last)) / 60)
            lines.append(f"Last full check: {ago} min ago · every {self.cfg.interval_min} min"
                         + (" · 🔕 muted" if self.monitor.muted() else ""))
        else:
            lines.append("First check still running…")
        lines.append("")
        for s in self.cfg.enabled_sites(self.store):
            ss = by_site.get(s.key, [])
            err = sum(1 for x in ss if x["last_error"])
            scanning = " ⏳" if s.key in self.monitor.scanning else ""
            lines.append(f"{s.flag} {esc(s.key)}: {counts.get(s.key, 0)} items · {len(ss)} sections"
                         + (f" · ⚠️{err} err" if err else "") + scanning)
        await self.tg.send("\n".join(lines), chat_id=chat)

    async def cmd_scan(self, chat, arg: str) -> None:
        key = arg.strip().lower()
        if key and key not in self.cfg.sites:
            return await self.tg.send(f"Unknown shop <code>{esc(key)}</code>. See /sites.", chat_id=chat)
        msg = await self.tg.send("⏳ Checking " + (self.cfg.sites[key].label if key else "all shops") + "…",
                                 chat_id=chat)
        results = await self.monitor.scan_all([key] if key else None)
        new = sum(r.get("events", 0) for r in results)
        errs = sum(r.get("errors", 0) for r in results)
        await self.tg.edit(chat, msg["message_id"], f"✅ Check finished — {new} alert(s) sent"
                           + (f", {errs} section error(s)" if errs else "") + ".")

    async def cmd_discover(self, chat, arg: str) -> None:
        key = arg.strip().lower()
        sites = [self.cfg.sites[key]] if key in self.cfg.sites else self.cfg.enabled_sites(self.store)
        msg = await self.tg.send(f"🔭 Re-discovering football sections on {len(sites)} shop(s)… this takes a few "
                                 f"minutes.", chat_id=chat)
        await self.monitor.scan_all([s.key for s in sites], force_discovery=True)
        await self.tg.edit(chat, msg["message_id"], "✅ Discovery finished. See /browse.")

    # ------------------------------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------------------------------
    async def on_callback(self, q: dict) -> None:
        msg = q.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if not self._authorised(chat):
            return await self.tg.answer(q["id"], "Private bot", alert=True)
        mid = msg.get("message_id")
        data = q.get("data", "")
        parts = data.split(":")
        head = parts[0]
        await self.tg.answer(q["id"])
        if head == "noop":
            return
        if head == "m":
            what = parts[1]
            if what == "search":
                return await self.tg.send("Type what you want to search — e.g. <code>world cup</code>, "
                                          "<code>megacracks @es</code>, <code>adrenalyn @it @uk</code>", chat_id=chat)
            if what == "stock":
                self.awaiting = "search_stock"
                return await self.tg.send("What should I look for (in-stock only)?", chat_id=chat)
            if what == "browse":
                return await self.cmd_browse(chat, "", mid)
            if what == "new":
                return await self.cmd_new(chat, "")
            if what == "deals":
                return await self.cmd_deals(chat, "")
            if what == "sites":
                return await self.cmd_sites(chat, "")
            if what == "status":
                return await self.cmd_status(chat, "")
        elif head == "bs":
            return await self._browse_site(chat, mid, parts[1], int(parts[2]))
        elif head == "bc":
            return await self._browse_source(chat, mid, int(parts[1]))
        elif head == "ts":
            if parts[1] == "*":
                for s in self.cfg.sites.values():
                    self.store.set(f"site.{s.key}.enabled", parts[2])
            else:
                s = self.cfg.sites[parts[1]]
                self.store.set(f"site.{s.key}.enabled", "0" if site_enabled(self.store, s) else "1")
            return await self.cmd_sites(chat, "", mid)
        elif head == "al":
            if parts[1] == "mute":
                self.store.set("muted", "0" if self.monitor.muted() else "1")
            else:
                self.store.set(f"alert.{parts[1]}", "0" if self.monitor.alert_on(parts[1]) else "1")
            return await self.cmd_alerts(chat, "", mid)
        elif head == "uw":
            for sid in parts[1].split(","):
                if sid.isdigit():
                    self.store.remove_source(int(sid))
            return await self.cmd_watchlist(chat, "", mid)
        elif head == "sr":
            sess = self.sessions.get(parts[1])
            if not sess:
                return await self.tg.edit(chat, mid, "This list expired — run the search again.")
            act = parts[2]
            if act == "p":
                sess.page = int(parts[3])
            elif act == "f":
                sess.only_stock = not sess.only_stock
                sess.page = 0
            elif act == "s":
                sess.site = None if parts[3] == "*" else parts[3]
                sess.page = 0
            elif act == "v":
                return await self._show_shop_picker(chat, mid, sess)
            return await self._show(chat, sess, mid, show_site=sess.back is None)

    # ------------------------------------------------------------------------------------------
    # result sessions
    # ------------------------------------------------------------------------------------------
    def _new_session(self, title: str, rows, *, only_stock: bool = False, back: str | None = None) -> ResultSession:
        sid = format(next(self._ids), "x")
        sess = ResultSession(sid, title, rows, only_stock=only_stock, back=back)
        self.sessions[sid] = sess
        while len(self.sessions) > 60:
            self.sessions.popitem(last=False)
        return sess

    def _keyboard(self, sess: ResultSession, pages: int) -> dict:
        rows = []
        if pages > 1:
            rows.append([button("◀", data=f"sr:{sess.id}:p:{max(0, sess.page - 1)}"),
                         button(f"{sess.page + 1}/{pages}", data="noop"),
                         button("▶", data=f"sr:{sess.id}:p:{min(pages - 1, sess.page + 1)}")])
        row = [button("📋 Show all" if sess.only_stock else "✅ In stock only", data=f"sr:{sess.id}:f")]
        if len({s.key for s, _ in sess.rows}) > 1:
            row.append(button("🏪 By shop" if not sess.site else "🌍 All shops",
                              data=f"sr:{sess.id}:v" if not sess.site else f"sr:{sess.id}:s:*"))
        rows.append(row)
        if sess.back:
            rows.append([button("⬅️ Back", data=sess.back)])
        return keyboard(rows)

    async def _translate_rows(self, rows: list[tuple[Site, Product]]) -> None:
        """Make the names on screen English (only the rows being shown; results are cached)."""
        if not self.tr:
            return
        groups: dict[str, list[Product]] = {}
        for site, p in rows:
            if not site.english and p.orig_name is None:
                groups.setdefault(site.key, []).append(p)

        async def one(key: str, prods: list[Product]) -> None:
            en = await self.tr.many([p.name for p in prods], source_lang=self.cfg.sites[key].lang, strict=True)
            for p, e in zip(prods, en):
                if e is not None:
                    p.orig_name, p.name = p.name, e
                else:
                    p.name = self.tr.rough(p.name)
        await asyncio.gather(*[one(k, v) for k, v in groups.items()])

    async def _show(self, chat, sess: ResultSession, message_id: int | None = None, show_site: bool = True) -> None:
        view = sess.view()
        pages = max(1, (len(view) + sess.per_page - 1) // sess.per_page)
        sess.page = min(sess.page, pages - 1)
        await self._translate_rows(view[sess.page * sess.per_page:(sess.page + 1) * sess.per_page])
        title = sess.title + (f" — {self.cfg.sites[sess.site].label}" if sess.site else "")
        text = render.result_page(title, view, page=sess.page, per_page=sess.per_page,
                                  show_site=show_site and not sess.site)
        if sess.notes:
            text += "\n\n" + sess.notes
        markup = self._keyboard(sess, pages)
        if message_id:
            await self.tg.edit(chat, message_id, text, markup=markup)
        else:
            await self.tg.send(text, chat_id=chat, markup=markup)

    async def _show_shop_picker(self, chat, mid: int, sess: ResultSession) -> None:
        counts: dict[str, list[int]] = {}
        for s, p in sess.rows:
            c = counts.setdefault(s.key, [0, 0])
            c[0] += 1
            c[1] += int(p.in_stock)
        rows, row = [], []
        for key, (n, n_in) in counts.items():
            site = self.cfg.sites[key]
            row.append(button(f"{site.flag} {n} (✅{n_in})", data=f"sr:{sess.id}:s:{key}"))
            if len(row) == 3:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([button("🌍 All shops", data=f"sr:{sess.id}:s:*")])
        await self.tg.edit(chat, mid, f"🏪 <b>{esc(sess.title)}</b> — pick a shop (✅ = in stock)",
                           markup=keyboard(rows))

    @staticmethod
    def _row_to_product(r) -> Product:
        import json
        return Product(site=r["site"], pid=r["pid"], name=r["name"], url=r["url"], price=r["price"],
                       old_price=r["old_price"], currency=r["currency"] or "", in_stock=bool(r["in_stock"]),
                       image=r["image"] or "", labels=json.loads(r["labels"] or "[]"),
                       orig_name=r["name_orig"])

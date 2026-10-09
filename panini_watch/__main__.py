"""Command line: python -m panini_watch <setup|run|scan|search|discover|sites>"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import __version__

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("PANINI_DATA") or ROOT / "data")


def _setup_logging(verbose: bool, to_file: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if sys.stderr is not None:          # no console when started hidden at login
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if to_file:
        DATA.mkdir(exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(DATA / "panini_watch.log", maxBytes=2_000_000, backupCount=3,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _env() -> tuple[str | None, str | None]:
    load_dotenv(ROOT / ".env")
    return os.getenv("TELEGRAM_BOT_TOKEN") or None, os.getenv("TELEGRAM_CHAT_ID") or None


def _load():
    from .sites import load_config
    from .store import Store
    cfg = load_config(ROOT / "config.yaml")
    store = Store(DATA / "panini.db")
    if store.get("alerts_version") != "2":      # one-time: only new listings + discounts, drop queued old alerts
        for kind, on in (("new", "1"), ("discount", "1"), ("restock", "0"), ("price_drop", "0"), ("sold_out", "0")):
            store.set(f"alert.{kind}", on)
        store.set_json("pending", [])
        store.set("alerts_version", "2")
    return cfg, store


# ----------------------------------------------------------------------------------------------
async def _services(store):
    """English translation + USD rates, shared by every command."""
    from . import models
    from .fx import Rates
    from .i18n import Translator
    tr, fx = Translator(store), Rates(store)
    await fx.refresh()
    models.FX = fx
    return tr, fx


async def _fx_loop(fx) -> None:
    while True:
        await asyncio.sleep(6 * 3600)
        await fx.refresh()


async def _prewarm(cfg, store, fetcher) -> None:
    """Pass every shop's cookie gate once, in the background, so searches are instant."""
    from .sites import site_enabled
    sites = [s for s in cfg.sites.values() if site_enabled(store, s)]
    probe = lambda s: s.base + ("products.json?limit=1" if s.kind == "shopify"
                                else "catalogsearch/result/?q=fifa&product_list_limit=12")
    await asyncio.gather(*[fetcher.prewarm(s.key, s.locale, probe(s)) for s in sites])
    logging.getLogger("panini").info("shop sessions warmed")


async def _translate_backlog(cfg, store, tr) -> None:
    """Translate product names already in the database (from before English mode existed)."""
    non_en = [s.key for s in cfg.sites.values() if not s.english]
    done = 0
    while not tr.is_down():
        rows = store.untranslated(non_en, 240)
        if not rows:
            break
        by_site: dict[str, list] = {}
        for r in rows:
            by_site.setdefault(r["site"], []).append(r)
        updates = []
        for key, rs in by_site.items():
            en = await tr.many([r["name"] for r in rs], source_lang=cfg.sites[key].lang, strict=True)
            updates += [(key, r["pid"], r["name"], e) for r, e in zip(rs, en) if e is not None]
        if updates:
            store.set_names(updates)
            done += len(updates)
        if not updates or tr.is_down():
            break
    if done:
        logging.getLogger("panini").info("translated %d stored product names to English", done)


async def cmd_setup(args) -> int:
    from .telegram import Telegram, TelegramError
    token, chat = _env()
    if not token:
        token = input("Paste your Telegram bot token (from @BotFather): ").strip()
    tg = Telegram(token, None)
    try:
        me = await tg.me()
    except TelegramError as e:
        print(f"❌ Telegram rejected that token: {e}")
        return 1
    print(f"✅ Bot found: @{me['username']}")
    if not chat:
        print(f"Now open Telegram, find @{me['username']} and send it the message /start  (waiting up to 3 min)…")
        offset = None
        import time
        end = time.time() + 180
        while time.time() < end and not chat:
            try:
                for u in await tg.updates(offset, timeout=10):
                    offset = u["update_id"] + 1
                    m = u.get("message")
                    if m and m.get("text"):
                        chat = str(m["chat"]["id"])
                        print(f"✅ Got your chat id: {chat} ({m['from'].get('first_name', '')})")
                        break
            except TelegramError as e:
                print("…", e)
                await asyncio.sleep(3)
        if not chat:
            print("❌ Did not receive a message. Run setup again and send /start to the bot.")
            return 1
    tg.chat_id = chat
    await tg.send("✅ <b>Panini Watch is connected.</b>\nSetup finished — I will start watching the shops.")
    (ROOT / ".env").write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\n", encoding="utf-8")
    print(f"💾 Saved to {ROOT / '.env'}")
    await tg.close()
    return 0


async def cmd_run(args) -> int:
    from .bot import Bot
    from .browser import Fetcher
    from .monitor import Monitor
    from .telegram import Telegram, TelegramError
    token, chat = _env()
    cfg, store = _load()
    if not token:
        print("No TELEGRAM_BOT_TOKEN found. Run:  python -m panini_watch setup")
        return 1
    chat = chat or store.get("chat_id")
    tg = Telegram(token, chat)
    try:
        me = await tg.me()
    except TelegramError as e:
        print(f"Telegram rejected the token: {e}")
        return 1
    log = logging.getLogger("panini")
    log.info("Panini Watch %s — bot @%s, chat %s", __version__, me["username"], chat or "(waiting for /start)")
    fetcher = Fetcher(page_delay=cfg.page_delay, max_sites_parallel=cfg.parallel)
    monitor = Monitor(cfg, store, fetcher, notify=None)  # type: ignore[arg-type]
    bot = Bot(tg, cfg, store, fetcher, monitor)
    tr, fx = await _services(store)
    monitor.tr = bot.tr = tr

    first_run = store.get("baseline_announced") is None

    async def on_cycle(results: list[dict]) -> None:
        await _translate_backlog(cfg, store, tr)     # retry anything the translator was too busy for
        if store.get("baseline_announced") is None and tg.chat_id:
            n = sum(r.get("products", 0) for r in results)
            ok = sum(1 for r in results if not r.get("errors"))
            await tg.send(f"✅ <b>First scan finished.</b> Tracking <b>{n}</b> football products on "
                          f"<b>{ok}/{len(results)}</b> shops.\nFrom now on you only get alerts for what is "
                          f"<i>new</i>. Try typing <code>world cup</code> or /browse.")
            store.set("baseline_announced", "1")

    monitor.on_cycle = on_cycle
    if tg.chat_id and first_run:
        n = len(cfg.enabled_sites(store))
        await tg.send(f"👋 <b>Panini Watch started.</b> Doing a first (silent) scan of {n} shops to learn what "
                      f"already exists — a few minutes. I will tell you when it is done.")
    async def warm_then_scan() -> None:
        await _translate_backlog(cfg, store, tr)
        await _prewarm(cfg, store, fetcher)
        await monitor.run_forever()

    try:
        await asyncio.gather(bot.run(), warm_then_scan(), _fx_loop(fx))
    finally:
        await fetcher.close()
        await tg.close()
        await tr.close()
    return 0


async def cmd_tick(args) -> int:
    """One scheduled cloud run: check every shop once, answer Telegram commands until the time budget ends."""
    import time
    from .bot import Bot
    from .browser import Fetcher
    from .monitor import Monitor
    from .telegram import Telegram
    start = time.time()
    deadline = start + args.budget
    token, chat = _env()
    cfg, store = _load()
    if not token:
        print("TELEGRAM_BOT_TOKEN missing")
        return 1
    chat = chat or store.get("chat_id")
    tg = Telegram(token, chat)
    fetcher = Fetcher(page_delay=cfg.page_delay, max_sites_parallel=cfg.parallel)
    monitor = Monitor(cfg, store, fetcher, notify=None)  # type: ignore[arg-type]
    monitor.allow_discovery = False
    bot = Bot(tg, cfg, store, fetcher, monitor)
    tr, fx = await _services(store)
    monitor.tr = bot.tr = tr
    log = logging.getLogger("panini")
    log.info("tick: budget %ds, chat %s", args.budget, chat or "(none yet)")

    async def scan() -> None:
        await monitor.flush_pending()
        await _prewarm(cfg, store, fetcher)
        results = await monitor.scan_all()
        await _translate_backlog(cfg, store, tr)
        n = sum(r.get("events", 0) for r in results)
        errs = sum(r.get("errors", 0) for r in results)
        log.info("tick scan finished in %ds: %d events, %d errors", time.time() - start, n, errs)

    try:
        bot_task = asyncio.create_task(bot.run(deadline=deadline))
        try:
            await asyncio.wait_for(scan(), timeout=max(60, args.budget - 30))
        except asyncio.TimeoutError:
            log.warning("scan did not finish inside the time budget; it continues next run")
        await bot_task
    finally:
        await fetcher.close()
        await tg.close()
        await tr.close()
    return 0


async def cmd_shipcheck(args) -> int:
    """Test at checkout which shops really ship to the destination country and store the verdicts."""
    from .browser import Fetcher
    from .shipping import run_check
    cfg, store = _load()
    dest = (args.country or cfg.raw.get("destination") or "AE").upper()
    f = Fetcher()
    try:
        await f.start()
        res = await run_check(f._browser, cfg, store, dest, args.site or None)
    finally:
        await f.close()
    ok = [k for k, v in res.items() if v["ships"]]
    print(f"\nShips to {dest}: {ok or 'none'}")
    return 0


async def cmd_scan(args) -> int:
    """One-off scan printing events to the console (no Telegram needed)."""
    from .browser import Fetcher
    from .monitor import Monitor
    cfg, store = _load()
    fetcher = Fetcher(page_delay=cfg.page_delay, max_sites_parallel=cfg.parallel, headless=not args.show)

    async def notify(site, events):
        for e in events:
            print(f"  [{e.type:10}] {site.key}: {e.product.name} | {e.product.price_text()} | "
                  f"{'in stock' if e.product.in_stock else 'sold out'} | {e.product.url}")
        return True

    mon = Monitor(cfg, store, fetcher, notify)
    tr, _fx = await _services(store)
    mon.tr = tr
    try:
        res = await mon.scan_all(args.site or None, force_discovery=args.rediscover)
        for r in res:
            print(r)
    finally:
        await fetcher.close()
    return 0


async def cmd_search(args) -> int:
    from .browser import Fetcher
    from .sites import live_search
    cfg, store = _load()
    keys = args.site or [s.key for s in cfg.enabled_sites(store)]
    fetcher = Fetcher(page_delay=cfg.page_delay, max_sites_parallel=cfg.parallel)
    tr, _fx = await _services(store)
    try:
        res = await asyncio.gather(*[live_search(fetcher, cfg.sites[k], args.term) for k in keys])
        for k, L in zip(keys, res):
            site = cfg.sites[k]
            shown = L.products[: args.limit]
            if not site.english and shown:
                for p, en in zip(shown, await tr.many([p.name for p in shown], source_lang=site.lang)):
                    p.name = en
            print(f"\n== {site.label}: {len(L.products)} {L.error}")
            for p in shown:
                print(f"  {'IN ' if p.in_stock else 'OUT'} {p.price_text():>26}  {p.name[:80]}")
    finally:
        await fetcher.close()
    return 0


async def cmd_discover(args) -> int:
    from .browser import Fetcher
    from .sites import discover
    cfg, store = _load()
    fetcher = Fetcher(page_delay=cfg.page_delay, max_sites_parallel=cfg.parallel)
    try:
        for k in args.site or [s.key for s in cfg.enabled_sites(store)]:
            added = await discover(fetcher, cfg.sites[k], store, log_fn=print)
            print(k, added)
            for s in store.sources(k):
                print(f"   {s['kind']:8} {s['mode']:6} {s['name'][:50]:50} {s['value'][-70:]}")
    finally:
        await fetcher.close()
    return 0


async def cmd_sites(args) -> int:
    cfg, store = _load()
    from .sites import site_enabled
    for s in cfg.sites.values():
        print(f"{'ON ' if site_enabled(store, s) else 'off'} {s.key:6} {s.label:38} {s.base}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="panini_watch", description="Panini football release watcher → Telegram")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="connect your Telegram bot")
    sub.add_parser("run", help="start watching + the Telegram bot")
    p = sub.add_parser("scan", help="one scan, print alerts to the console")
    p.add_argument("--site", action="append")
    p.add_argument("--rediscover", action="store_true")
    p.add_argument("--show", action="store_true", help="show the browser window")
    p = sub.add_parser("tick", help="one scheduled cloud run (scan + answer commands for --budget seconds)")
    p.add_argument("--budget", type=int, default=500)
    p = sub.add_parser("shipcheck", help="test which shops really ship to your country (weekly in the cloud)")
    p.add_argument("--country", help="ISO code, default from config.yaml (AE)")
    p.add_argument("--site", action="append")
    p = sub.add_parser("search", help="live search from the console")
    p.add_argument("term")
    p.add_argument("--site", action="append")
    p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("discover", help="(re)discover football categories")
    p.add_argument("--site", action="append")
    sub.add_parser("sites", help="list shops")
    args = ap.parse_args()
    _setup_logging(args.verbose, to_file=args.cmd in ("run", "tick"))
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    fn = {"setup": cmd_setup, "run": cmd_run, "tick": cmd_tick, "shipcheck": cmd_shipcheck, "scan": cmd_scan, "search": cmd_search,
          "discover": cmd_discover, "sites": cmd_sites}[args.cmd]
    try:
        raise SystemExit(asyncio.run(fn(args)))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

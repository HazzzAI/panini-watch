"""Does a shop really ship to a destination country (UAE)?

Shops list the UAE in their country dropdown and still answer "no quotes available" at checkout, so the
only reliable test is the one a customer does: put a real in-stock item in the cart, open the cart's
"Estimate shipping" box, choose the destination country and see whether shipping methods appear.
No order is placed and no personal data is entered (only a country, and a neutral region/postcode).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import Browser, Page

from .sites import Site

log = logging.getLogger("panini.shipping")

_DECLINE = ["#CybotCookiebotDialogBodyButtonDecline", "#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll",
            "button:has-text('Reject all')", "button:has-text('Rechazar')", "button:has-text('Alle ablehnen')",
            "button:has-text('Tout refuser')", "button:has-text('Rifiuta')", "button:has-text('Weigeren')"]
_ADD = ["#product-addtocart-button", "button.tocart", "button[title*='cart' i]", "button[title*='carrito' i]"]
_TITLE = ["#block-shipping .title", "#block-shipping [data-role=title]", ".block.shipping .title",
          "#block-shipping .summary-title"]
_POSITIVE = "input[name='shipping_method'], .table-checkout-shipping-method input[type=radio], " \
            ".table-checkout-shipping-method tbody tr"
_NEGATIVE = ".no-quotes-block"

# the shop's own country, used as a positive control ("the estimator works for the home market")
HOME = {"es": "ES", "it": "IT", "fr": "FR", "de": "DE", "uk": "GB", "pt": "PT", "nl": "NL", "be": "BE", "ch": "CH",
        "pl": "PL", "ro": "RO", "hu": "HU", "gr": "GR", "dk": "DK", "no": "NO", "se": "SE", "fi": "FI", "il": "IL",
        "int": "DE", "hobby": "DE", "br": "BR", "mx": "MX", "cl": "CL", "uy": "UY", "ar": "AR", "co": "CO"}


@dataclass
class ShipResult:
    site: str
    ships: bool | None            # True / False / None = could not tell
    reason: str = ""
    methods: list[str] = field(default_factory=list)
    control_ok: bool | None = None
    checked_with: str = ""


async def _decline_cookies(pg: Page) -> None:
    try:
        await pg.wait_for_selector("#CybotCookiebotDialog", timeout=9000)
    except Exception:
        return
    for sel in _DECLINE:
        try:
            el = await pg.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await pg.wait_for_timeout(1200)
                return
        except Exception:
            continue


async def _first_visible(pg: Page, selectors: list[str]):
    for sel in selectors:
        try:
            el = await pg.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    return None


_JS_STATE = r"""() => {
  const vis = n => !!(n.offsetParent) && getComputedStyle(n).display !== 'none';
  const radios = [...document.querySelectorAll('input[type=radio]')].filter(r => /^s_method_/.test(r.id) && vis(r));
  const methods = radios.map(r => (r.closest('tr,li,div') || r).innerText.trim().replace(/\s+/g, ' ').slice(0, 160));
  const notes = [...document.querySelectorAll('p.field.note, .no-quotes-block')].filter(vis).length;
  return {methods, notes};
}"""


async def _quote(pg: Page, country: str, *, extra: bool = False) -> tuple[str, list[str]]:
    """Select `country` in the cart estimator -> ('methods', [labels]) | ('none', []) | ('unknown', [])."""
    est = await pg.query_selector("select[name=country_id]")
    if est is None:
        try:
            await pg.wait_for_selector("select[name=country_id]", state="attached", timeout=8000)
            est = await pg.query_selector("select[name=country_id]")
        except Exception:
            return "unknown", []
    if est is None:
        return "unknown", []
    if not await est.evaluate("(e, c) => [...e.options].some(o => o.value === c)", country):
        return "absent", []                                  # the shop does not even offer this country
    try:
        await est.select_option(country, timeout=4000)
    except Exception:                                        # hidden single-country selector: set it directly
        await est.evaluate("(e, c) => { e.value = c; e.dispatchEvent(new Event('change', {bubbles: true})); }", country)
    if extra:                                    # some carriers want a region / postcode: give neutral ones
        for sel, val in (("input[name=postcode]", "00000"), ("input[name=region]", "Dubai")):
            el = await pg.query_selector(sel)
            if el and await el.is_visible():
                await el.fill(val)
        await pg.keyboard.press("Tab")
    await pg.wait_for_timeout(2500)
    state = {"methods": [], "notes": 0}
    for _ in range(22):                          # up to ~11 s more for the quote to come back
        state = await pg.evaluate(_JS_STATE)
        if state["methods"]:
            await pg.wait_for_timeout(1500)      # let the price finish rendering before reading it
            state = await pg.evaluate(_JS_STATE)
            return "methods", state["methods"][:4]
        if state["notes"] >= 2:                  # prompt + "no quote available"
            await pg.wait_for_timeout(1500)      # make sure a late answer does not follow
            state = await pg.evaluate(_JS_STATE)
            return ("methods", state["methods"][:4]) if state["methods"] else ("none", [])
        await pg.wait_for_timeout(500)
    return "unknown", []


async def check_ships_to(browser: Browser, site: Site, product_url: str, dest: str = "AE") -> ShipResult:
    res = ShipResult(site.key, None, checked_with=product_url)
    ctx = await browser.new_context(locale=site.locale, viewport={"width": 1300, "height": 1000})
    await ctx.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font")
                    else route.continue_())
    pg = await ctx.new_page()
    pg.set_default_timeout(40000)
    try:
        await pg.goto(product_url, wait_until="domcontentloaded")
        await _decline_cookies(pg)
        add = None
        for _ in range(10):
            add = await _first_visible(pg, _ADD)
            if add:
                break
            await pg.wait_for_timeout(500)
        if add is None:
            res.reason = "no add-to-cart button"
            return res
        try:
            await add.click(timeout=8000)
        except Exception:                                    # a pop-up (newsletter / other cookie bar) is in the way:
            await pg.evaluate("() => document.querySelectorAll('.modal-popup, .modals-overlay, .amgdprcookie-bar-container, .amgdprjs-bar-template, .newsletter-popup').forEach(n => n.remove())")
            await add.evaluate("e => e.click()")
        await pg.wait_for_timeout(5000)
        await pg.goto(site.base + "checkout/cart/", wait_until="domcontentloaded")
        await pg.wait_for_timeout(5000)
        if await pg.query_selector(".cart-empty, .cart.empty"):
            res.reason = "cart stayed empty"
            return res
        title = await _first_visible(pg, _TITLE)
        if title:
            await title.click()
            await pg.wait_for_timeout(1200)
        state, labels = await _quote(pg, dest)               # destination first (no stale answer on screen)
        if state == "absent":
            res.ships, res.reason, res.control_ok = False, "shop does not offer this country at checkout", True
            return res
        if state == "none":                                  # second chance with a neutral region / postcode
            state, labels = await _quote(pg, dest, extra=True)
        if state == "methods":
            res.ships, res.methods, res.reason = True, labels, "shipping methods offered"
        elif state == "none":
            res.ships, res.reason = False, "shop answers: no shipping quote available"
        else:
            res.reason = "estimator did not answer"
        try:
            ctrl, _ = await _quote(pg, HOME.get(site.key, "DE"))  # control: the home market must produce a quote
            res.control_ok = ctrl == "methods"
        except Exception:                                      # noqa: BLE001  (never let the control undo a verdict)
            res.control_ok = None
        return res
    except Exception as e:                                # noqa: BLE001
        res.reason = f"error: {str(e)[:120]}"
        return res
    finally:
        await ctx.close()


# ---------------------------------------------------------------------------------------------
# whole-shop verdict (several products), Shopify variant, fee estimates
# ---------------------------------------------------------------------------------------------
import asyncio
import json
import time

_MONEY = re.compile(r"(?:[€£]|CHF|EUR)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:[€£]|CHF|EUR)")


def parse_fee(label: str) -> float | None:
    """'Envío por mensajero 50,00 €' / 'Express courier (...) €60' -> 50.0 / 60.0 (last amount in the label)."""
    found = [m.group(1) or m.group(2) for m in _MONEY.finditer(label)]
    if not found:
        return None
    raw = found[-1]
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if len(raw.split(",")[-1]) <= 2 else raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def summarize(results: list[tuple[ShipResult, float]], dest: str) -> dict:
    yes = [(r, p) for r, p in results if r.ships is True]
    no = [(r, p) for r, p in results if r.ships is False]
    if yes:
        samples = []
        for r, price in yes:
            fee = parse_fee(r.methods[0]) if r.methods else None
            if fee is not None and fee > 0:          # a 0.00 fee to another continent is a half-rendered price
                samples.append({"cart": price, "fee": fee})
        verdict, note = True, "ships to " + dest
        if no:
            note += " (not on every order: small orders get no quote)"
    elif no and (any(r.control_ok for r, _ in no) or all("does not offer" in r.reason for r, _ in no)):
        verdict, samples, note = False, [], "no shipping quote for " + dest
    else:
        verdict, samples = None, []
        note = "could not be verified (" + (results[0][0].reason if results else "no data") + ")"
    return {"ships": verdict, "note": note, "samples": sorted(samples, key=lambda x: x["cart"]),
            "dest": dest, "checked": time.time()}


async def _shopify_check(browser: Browser, site: Site, product_urls: list[str], dest: str) -> dict:
    last: dict = {"ships": None, "note": "could not be verified", "samples": [], "dest": dest, "checked": time.time()}
    for url in product_urls:
        last = await _shopify_one(browser, site, url, dest)
        if last["ships"] is not None:
            return last
    return last


async def _shopify_one(browser: Browser, site: Site, product_url: str, dest: str) -> dict:
    handle = product_url.rstrip("/").split("/products/")[-1].split("?")[0]
    ctx = await browser.new_context(locale=site.locale)
    try:
        pg = await ctx.new_page()
        await pg.goto(site.base, wait_until="domcontentloaded")
        await pg.wait_for_timeout(3000)
        names = {"AE": ("United Arab Emirates", "Dubai"), "SA": ("Saudi Arabia", "Riyadh"), "QA": ("Qatar", "Doha")}
        cname, prov = names.get(dest, (dest, ""))
        js = """async ([handle, cname, prov, home]) => {
            const pj = await (await fetch('/products/' + handle + '.json')).json();
            await fetch('/cart/add.js', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({id: pj.product.variants[0].id, quantity: 1})});
            const q = async (c, p) => (await (await fetch('/cart/shipping_rates.json?shipping_address[country]=' +
                encodeURIComponent(c) + '&shipping_address[province]=' + encodeURIComponent(p) +
                '&shipping_address[zip]=00000')).json());
            return {dest: await q(cname, prov), home: await q(home[0], home[1])}; }"""
        out = await pg.evaluate(js, [handle, cname, prov, ["Colombia", "Bogota D.C."]])
        has_dest = bool((out.get("dest") or {}).get("shipping_rates"))
        has_home = bool((out.get("home") or {}).get("shipping_rates"))
        if has_dest:
            return {"ships": True, "note": "ships to " + dest, "samples": [], "dest": dest, "checked": time.time()}
        if has_home:
            return {"ships": False, "note": "no shipping quote for " + dest, "samples": [], "dest": dest,
                    "checked": time.time()}
        return {"ships": None, "note": "could not be verified", "samples": [], "dest": dest, "checked": time.time()}
    finally:
        await ctx.close()


async def run_check(browser: Browser, cfg, store, dest: str = "AE", only: list[str] | None = None,
                    say=print) -> dict[str, dict]:
    """Test every shop (or `only`) and store the verdicts in the database as ship.<site>."""
    sem = asyncio.Semaphore(4)
    out: dict[str, dict] = {}

    async def one(site: Site) -> None:
        picks = store.sealed_products(site.key, 4)
        if not picks:
            out[site.key] = {"ships": None, "note": "no in-stock product known yet", "samples": [], "dest": dest,
                             "checked": time.time()}
            return
        async with sem:
            if site.kind == "shopify":
                try:
                    out[site.key] = await _shopify_check(browser, site, [r["url"] for r in picks[::-1]], dest)
                except Exception as e:                       # noqa: BLE001
                    out[site.key] = {"ships": None, "note": f"error: {str(e)[:80]}", "samples": [], "dest": dest,
                                     "checked": time.time()}
                return
            results = []
            for r in picks:
                res = await check_ships_to(browser, site, r["url"], dest)
                results.append((res, float(r["price"])))
                if res.ships is False and "does not offer" in res.reason:        # definitive: country not offered
                    break
                if len(results) >= 2 and all(x.ships is False for x, _ in results) and any(x.control_ok for x, _ in results):
                    break                                                         # two clear "no" answers are enough
            out[site.key] = summarize(results, dest)

    sites = [s for s in cfg.sites.values() if not only or s.key in only]
    await asyncio.gather(*[one(s) for s in sites])
    for key, v in out.items():
        store.set_json(f"ship.{key}", v)
        say(f"{key:6} ships={v['ships']!s:5} {v['note']} {v['samples']}")
    store.set("ship.dest", dest)
    store.set("ship.checked", str(time.time()))
    return out


def fee_for(ship: dict | None, price: float | None) -> float | None:
    """Shipping fee estimate for a cart worth `price` (shop currency), rounded UP to the next sampled quote."""
    if not ship or not ship.get("samples") or price is None:
        return None
    for smp in sorted(ship["samples"], key=lambda x: x["cart"]):
        if smp["cart"] >= price:
            return smp["fee"]
    return max(ship["samples"], key=lambda x: x["cart"])["fee"]

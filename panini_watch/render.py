"""Turn products and events into Telegram messages (HTML parse mode)."""
from __future__ import annotations

import re

from .models import Product
from .sites import Site
from .telegram import button, esc, esc_attr, keyboard

STOCK_ICON = {True: "✅", False: "❌"}


def stock_text(p: Product) -> str:
    return "✅ In stock" if p.in_stock else "❌ Sold out"


def price_line(p: Product) -> str:
    s = p.price_text()
    if p.discount_pct:
        s += f"  <s>{esc(Product(p.site, '', '', '', price=p.old_price, currency=p.currency).price_text(usd=False))}</s> (-{p.discount_pct}%)"
    return s


def product_card(kind: str, site: Site, p: Product, *, extra: str = "", category: str = "") -> tuple[str, dict]:
    head = {
        "new": f"🆕 <b>New on Panini {esc(site.label)}</b>",
        "restock": f"🔔 <b>Back in stock — {esc(site.label)}</b>",
        "discount": f"🏷 <b>DISCOUNT -{p.discount_pct}% — {esc(site.label)}</b>" if p.discount_pct else
                    f"🏷 <b>Discount — {esc(site.label)}</b>",
        "price_drop": f"💸 <b>Price drop — {esc(site.label)}</b>",
        "sold_out": f"🚫 <b>Sold out — {esc(site.label)}</b>",
    }.get(kind, f"ℹ️ <b>{esc(site.label)}</b>")
    lines = [head, "", f"<b>{esc(p.name)}</b>", f"💶 {price_line(p)}   {stock_text(p)}"]
    if extra:
        lines.append(extra)
    if p.labels:
        lines.append("🏷 " + esc(", ".join(p.labels)))
    if category:
        lines.append(f"📂 {esc(category)}")
    text = "\n".join(lines)
    markup = keyboard([[button("🔗 Open product", url=p.url)]])
    return text, markup


def digest(kind: str, site: Site, items: list[Product], *, per_message: int = 12) -> list[tuple[str, dict | None]]:
    title = {"new": f"🆕 <b>{len(items)} new products on {esc(site.label)}</b>",
             "restock": f"🔔 <b>{len(items)} back in stock on {esc(site.label)}</b>",
             "discount": f"🏷 <b>{len(items)} discounted products on {esc(site.label)}</b>",
             "price_drop": f"💸 <b>{len(items)} price drops on {esc(site.label)}</b>"}.get(kind, esc(site.label))
    out = []
    for i in range(0, len(items), per_message):
        chunk = items[i:i + per_message]
        lines = [title if i == 0 else f"{title} (cont.)", ""]
        for p in chunk:
            lines.append(f'{STOCK_ICON[p.in_stock]} <a href="{esc_attr(p.url)}">{esc(p.name)}</a> — {price_line(p)}')
        out.append(("\n".join(lines), None))
    return out


def result_page(title: str, rows: list[tuple[Site, Product]], *, page: int, per_page: int = 8,
                show_site: bool = True) -> str:
    """Compact list for search / browse results."""
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * per_page:(page + 1) * per_page]
    n_in = sum(1 for _, p in rows if p.in_stock)
    lines = [f"<b>{esc(title)}</b>", f"{total} found · ✅ {n_in} in stock · ❌ {total - n_in} sold out"
             + (f" · page {page + 1}/{pages}" if pages > 1 else ""), ""]
    for site, p in chunk:
        prefix = f"{site.flag} " if show_site else ""
        lines.append(f'{STOCK_ICON[p.in_stock]} {prefix}<a href="{esc_attr(p.url)}">{esc(p.name[:90])}</a>\n      {price_line(p)}')
    if not chunk:
        lines.append("Nothing matched.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# One product that appears in several shops at once = ONE message
# ---------------------------------------------------------------------------------------------
def group_key(p: Product) -> str:
    sku = (p.sku or "").split("_")[0].upper()
    if len(sku) >= 6 and re.search(r"\d", sku):
        return "sku:" + sku
    return "name:" + re.sub(r"[^a-z0-9]+", "", p.name.lower())


_HEAD = {"new": ("🆕", "New listing"), "discount": ("🏷", "DISCOUNT"), "restock": ("🔔", "Back in stock"),
         "price_drop": ("💸", "Price drop"), "sold_out": ("🚫", "Sold out")}


def group_card(kind: str, evs: list) -> tuple[str, dict]:
    """evs: events of the same product (one per shop). Single shop -> the normal card."""
    first = evs[0]
    if len(evs) == 1:
        extra = ""
        if kind == "price_drop" and first.old_price:
            extra = "was " + esc(Product(first.site.key, "", "", "", price=first.old_price,
                                         currency=first.product.currency).price_text())
        return product_card(kind, first.site, first.product, extra=extra, category=first.category)
    icon, label = _HEAD.get(kind, ("ℹ️", kind))
    lines = [f"{icon} <b>{label} — {len(evs)} shops</b>", "", f"<b>{esc(first.product.name)}</b>", ""]
    for e in evs:
        p = e.product
        lines.append(f'{e.site.flag} <a href="{esc_attr(p.url)}">{esc(e.site.name)}</a>  '
                     f'{STOCK_ICON[p.in_stock]} {price_line(p)}')
    return "\n".join(lines), keyboard([[button("🔗 Open first shop", url=first.product.url)]])


def group_digest(kind: str, groups: list, *, per_message: int = 10) -> list[str]:
    icon, label = _HEAD.get(kind, ("ℹ️", kind))
    out = []
    for i in range(0, len(groups), per_message):
        chunk = groups[i:i + per_message]
        lines = [f"{icon} <b>{len(groups)} {label.lower()} alerts</b>" + (" (cont.)" if i else ""), ""]
        for g in chunk:
            p = g[0].product
            flags = "".join(e.site.flag for e in g)
            lines.append(f'{STOCK_ICON[p.in_stock]} <a href="{esc_attr(p.url)}">{esc(p.name[:80])}</a> {flags} — '
                         f'{price_line(p)}')
        out.append("\n".join(lines))
    return out

"""HTML parsing for Panini's Magento storefronts (listing, search and navigation pages)."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from bs4 import BeautifulSoup, Tag

from .models import Product

_NUM_RE = re.compile(r"\d[\d\s  .,]*\d|\d")
_LABEL_SELECTORS = (
    ".product-labels, .product-label, .pnn-label, .label-text, .badge, .product-item-photo .label"
)


def _text(el: Tag | None) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _amount(el: Tag | None) -> float | None:
    if el is None:
        return None
    holder = el if el.has_attr("data-price-amount") else el.select_one("[data-price-amount]")
    if holder is not None:
        try:
            return float(holder["data-price-amount"])
        except (KeyError, ValueError):
            pass
    return _parse_price_text(_text(el))[0]


def _parse_price_text(txt: str) -> tuple[float | None, str]:
    m = _NUM_RE.search(txt)
    if not m:
        return None, ""
    raw = re.sub(r"[\s  ]", "", m.group(0))
    # decimal separator = the last "." or "," that has 1-2 digits after it
    last = max(raw.rfind(","), raw.rfind("."))
    if last != -1 and len(raw) - last - 1 <= 2 and (raw.count(",") + raw.count(".") == 1 or
                                                     raw[last] != raw[last - 1:last]):
        ip, fp = raw[:last], raw[last + 1:]
        num = f"{re.sub(r'[.,]', '', ip)}.{fp}"
    else:
        num = re.sub(r"[.,]", "", raw)
    try:
        value = float(num)
    except ValueError:
        return None, ""
    rest = (txt[:m.start()] + " " + txt[m.end():]).split("/")[0]
    cur = re.sub(r"[\s ‏‎]+", " ", rest).strip(" -")
    return value, cur


def parse_listing(html: str, site: str, page_url: str) -> tuple[list[Product], int | None, str | None]:
    """Return (products, total_claimed_by_page, next_page_url)."""
    soup = BeautifulSoup(html, "lxml")
    grid = soup.select_one("div.products, ol.products, ul.products")
    items = grid.select("li.product-item, li.item.product, div.product-item") if grid else []
    products: list[Product] = []
    seen: set[str] = set()
    for li in items:
        prod = _parse_item(li, site, page_url)
        if prod and prod.pid not in seen:
            seen.add(prod.pid)
            products.append(prod)

    total = None
    amount = soup.select_one("#toolbar-amount, .toolbar-amount")
    if amount:
        nums = [int(n.get_text(strip=True)) for n in amount.select(".toolbar-number") if n.get_text(strip=True).isdigit()]
        if len(nums) >= 3:
            total = nums[2]
        elif len(nums) == 1:
            total = nums[0]
    nxt = soup.select_one(".pages a.action.next, .pages-item-next a")
    next_url = urljoin(page_url, nxt["href"]) if nxt and nxt.get("href") else None
    return products, total, next_url


def _parse_item(li: Tag, site: str, page_url: str) -> Product | None:
    link = li.select_one(".product-item-name a") or li.select_one("a.product-item-link[href]")
    if not link or not link.get("href"):
        return None
    name = _text(link) or link.get("title", "")
    if not name:
        alt = li.select_one("img.product-image-photo")
        name = re.sub(r"^[^_]*_", "", alt.get("alt", "")) if alt else ""
    url = urljoin(page_url, link["href"])

    box = li.select_one("[data-price-box]")
    pid = ""
    if box is not None:
        pid = box.get("data-product-id") or box.get("data-price-box", "").replace("product-id-", "")
    form = li.select_one("form[data-product-sku]")
    sku = form.get("data-product-sku", "") if form else ""
    if not pid:
        inp = li.select_one("input[name=product]")
        pid = inp.get("value", "") if inp else ""
    if not pid:
        pid = sku or urlparse(url).path.rsplit("/", 1)[-1]

    price = old = None
    currency = ""
    if box is not None:
        special = box.select_one(".special-price")
        oldel = box.select_one(".old-price")
        if special is not None:
            price = _amount(special)
        else:
            price = _amount(box.select_one("[data-price-type=finalPrice]")) or _amount(box)
        if oldel is not None:
            old = _amount(oldel)
        _, currency = _parse_price_text(_text(box.select_one(".price")))
    if price is None:
        price, currency = _parse_price_text(_text(li.select_one(".price")))

    unavailable = li.select_one(".stock.unavailable, .out-of-stock, .stock.out-of-stock") is not None
    can_buy = li.select_one("form[data-role=tocart-form], button.tocart") is not None
    in_stock = (not unavailable) and (can_buy or li.select_one(".stock.available") is not None)

    img = li.select_one("img.product-image-photo")
    image = ""
    if img is not None:
        image = img.get("src") or img.get("data-src") or ""
        image = urljoin(page_url, image)
        # ask the store's image resizer for a larger picture than the 222px thumbnail
        image = re.sub(r"([?&])height=\d+", r"\g<1>height=600", image)
        image = re.sub(r"([?&])width=\d+", r"\g<1>width=600", image)
        image = re.sub(r"canvas=\d+:\d+", "canvas=600:600", image)

    labels = []
    for el in li.select(_LABEL_SELECTORS):
        t = _text(el)
        if t and t not in labels and len(t) < 40:
            labels.append(t)

    return Product(site=site, pid=str(pid), name=name, url=url, price=price, old_price=old,
                   currency=currency, in_stock=in_stock, image=image, sku=sku, labels=labels,
                   source=page_url)


def with_query(url: str, **params: str | int) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q.update({k: str(v) for k, v in params.items()})
    return urlunparse(parts._replace(query=urlencode(q)))


_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".webp", ".svg", ".css", ".js")


def parse_nav_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Collect (text, url) of internal links from the storefront navigation menu."""
    soup = BeautifulSoup(html, "lxml")
    roots = soup.select(r"nav, .navigation, .nav-sections, #store\.menu, .menu, .sections") or [soup]
    host = urlparse(base_url).netloc.replace("www.", "")
    out: dict[str, str] = {}
    for root in roots:
        for a in root.select("a[href]"):
            href = a["href"].split("#")[0].strip()
            if not href or href.startswith(("javascript", "mailto", "tel")):
                continue
            url = urljoin(base_url, href)
            parts = urlparse(url)
            if parts.netloc.replace("www.", "") != host or parts.path.lower().endswith(_SKIP_EXT):
                continue
            if parts.path.rstrip("/") == urlparse(base_url).path.rstrip("/"):
                continue
            if any(x in parts.path for x in ("/customer/", "/checkout/", "/wishlist", "/catalogsearch/")):
                continue
            txt = _text(a)
            if url not in out or (txt and len(txt) > len(out[url]) and len(txt) < 60):
                out[url] = txt
    return [(t, u) for u, t in out.items()]

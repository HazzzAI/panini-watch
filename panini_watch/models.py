from __future__ import annotations

from dataclasses import dataclass, field


# Site keys whose shop writes prices as "12,50" (filled from config.yaml locales at start-up).
COMMA_SITES: set[str] = set()
SITE_ISO: dict[str, str] = {}          # site key -> ISO currency code (filled from config.yaml)
FX = None                              # fx.Rates instance, set at start-up (None = no USD shown)


@dataclass
class Product:
    site: str                 # site key, e.g. "es"
    pid: str                  # store product id (unique within a site)
    name: str
    url: str
    price: float | None = None
    old_price: float | None = None
    currency: str = ""
    in_stock: bool = True
    image: str = ""
    sku: str = ""
    labels: list[str] = field(default_factory=list)   # e.g. "Pre-order", "New"
    source: str = ""          # listing/search URL it was found on
    meta: str = ""            # extra text used only for football matching (tags, type)
    orig_name: str | None = None   # name in the shop's own language (name itself is English)

    @property
    def discount_pct(self) -> int | None:
        if self.price and self.old_price and self.old_price > self.price:
            return round((1 - self.price / self.old_price) * 100)
        return None

    def price_text(self, usd: bool = True) -> str:
        if self.price is None:
            return "—"
        txt = f"{self.price:,.2f}"                     # 46,900.00
        if self.site in COMMA_SITES:
            txt = txt.replace(",", " ").replace(".", ",")   # 46 900,00
        out = f"{txt} {self.currency}".strip()
        iso = SITE_ISO.get(self.site, "")
        if usd and FX is not None and iso and iso != "USD":
            v = FX.to_usd(self.price, iso)
            if v is not None:
                out += f" (≈ ${v:,.2f})"
        return out


@dataclass
class Listing:
    """Result of reading one listing / search page (all of its paginated pages)."""
    url: str
    products: list[Product]
    total: int | None = None     # total the page claims to have
    complete: bool = True        # False if we could not read every page
    error: str = ""

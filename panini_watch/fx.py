"""Currency conversion to USD (free daily rates, cached; falls back to rough built-in rates)."""
from __future__ import annotations

import json
import logging
import time

import httpx

from .store import Store

log = logging.getLogger("panini.fx")

# Rough fallback (units per 1 USD) used only if the live rates have never been downloaded.
_FALLBACK = {"USD": 1.0, "EUR": 0.86, "GBP": 0.75, "CHF": 0.80, "PLN": 3.7, "RON": 4.4, "HUF": 340.0, "DKK": 6.4,
             "NOK": 10.5, "SEK": 9.6, "ILS": 3.4, "BRL": 5.4, "MXN": 18.5, "CLP": 940.0, "UYU": 40.0,
             "ARS": 1200.0, "COP": 4000.0}

_URL = "https://open.er-api.com/v6/latest/USD"


class Rates:
    def __init__(self, store: Store | None = None):
        self.store = store
        self.rates: dict[str, float] = dict(_FALLBACK)
        self.updated: float = 0.0
        if store:
            cached = store.get_json("fx_rates")
            if cached:
                self.rates.update(cached["rates"])
                self.updated = cached["ts"]

    async def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self.updated < 12 * 3600:
            return
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                data = (await c.get(_URL)).json()
            if data.get("result") == "success":
                self.rates.update({k: float(v) for k, v in data["rates"].items()})
                self.updated = time.time()
                if self.store:
                    self.store.set_json("fx_rates", {"ts": self.updated, "rates": data["rates"]})
                log.info("FX rates updated (%d currencies)", len(data["rates"]))
        except Exception as e:                      # keep the old rates
            log.warning("FX refresh failed: %s", e)

    def to_usd(self, amount: float, iso: str) -> float | None:
        rate = self.rates.get(iso.upper())
        return amount / rate if rate else None

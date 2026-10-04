# ⚽ Panini Watch

Watches **every official Panini shop** (26 storefronts: Spain, Italy, France, Germany, UK, Portugal, Netherlands,
Belgium, Switzerland, Poland, Romania, Hungary, Greece, Denmark, Norway, Sweden, Finland, Israel, the international
store, Panini Hobby, Brazil, Mexico, Chile, Uruguay, Argentina, Colombia) and sends a **Telegram message** when a
**football** product

* 🆕 appears for the first time (new album, Megacracks box, Adrenalyn bundle, Este stickers …)
* 🔔 comes **back in stock**
* 🏷 goes **on discount** (sale tag with the old price crossed out) · 💸 gets **cheaper** (≥10 %)

Everything is shown **in English** (product and category names are translated from each shop's language) and
every price that is not in dollars gets its **USD value** next to it, e.g. `129,00 € (≈ $145.14)`.

It is also an interactive bot: type *world cup* and it searches all shops live and shows what is
✅ in stock / ❌ sold out; browse shop → category → product; ask for the newest finds, etc.

Only football is kept (World Cup, FIFA 365, LaLiga/Megacracks/Este/Adrenalyn, Calciatori, Bundesliga, Premier
League, Libertadores …). NBA, comics, manga, Disney, cycling, … are filtered out.
Single "missing card" listings (`cartas faltantes`, `mancanti`, …) are ignored — they are not releases.
**Panini America (US)** is not included: its site blocks automated access with a hard 403.

## Setup (once)

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. Double-click **`setup.bat`**. Paste the token, then send `/start` to your bot in Telegram.
   The chat id is detected automatically and saved in `.env`.
3. Double-click **`run.bat`**. The first run does a silent scan of all shops (learning what already exists, ~30–60
   min); the bot tells you when it is done. After that you only get alerts for what is *new*.
4. Optional: **`install_autostart.bat`** starts it hidden every time you log in to Windows.

> **Free 24/7 mode (no PC needed)**: it runs on GitHub Actions — repo `HazzzAI/panini-watch`, workflow `watch.yml`.
> Every run (back-to-back, ~9 min each) checks all shops, sends alerts and answers bot commands; the database lives
> on the `state` branch; the bot token is an encrypted repository secret. Watch it at
> https://github.com/HazzzAI/panini-watch/actions . Do **not** also run `run.bat` on the PC (one listener per bot).
> A paid-server alternative (instant replies) is in [DEPLOY.md](DEPLOY.md).

## Using the bot

| You type | What happens |
|---|---|
| `world cup` | live search on every enabled shop, ✅/❌ stock, price, link; buttons: in-stock only, by shop, pages |
| `megacracks @es` | search only the Spanish shop (`@es @it @uk …`) |
| `/stock este @es` | same, in-stock only |
| `/browse` | shops → football sections (LaLiga, Megacracks, Liga Este, World Cup 2026 …) → products |
| `/deals` · `/deals es` | everything currently on discount (biggest % first) |
| `/new` · `/new es 3` | newest products found (last 7 days / for a shop / last 3 days) |
| `/watch Megacracks` | always alert on this keyword (all shops, or `@es`); `/watchlist` to remove |
| *(paste a Panini category link)* | start watching that exact page |
| `/sites` | switch shops on/off |
| `/alerts` | choose new / restock / price-drop / sold-out alerts, mute |
| `/status`, `/scan [shop]`, `/discover [shop]` | health, check now, re-detect categories |

Shop codes: `es it fr de uk pt nl be ch pl ro hu gr dk no se fi il int hobby br mx cl uy ar co`.

## Console tools (no Telegram needed)

```
python -m panini_watch sites                      # list shops
python -m panini_watch search "world cup" --site es --site it
python -m panini_watch discover --site es         # show the football sections it will watch
python -m panini_watch scan --site es             # one scan, alerts printed to the console
```

## How it works

* Panini's shops share one Magento platform behind a JavaScript/cookie gate, so pages are read with a real headless
  Chrome (Playwright) — slowly (one page at a time per shop, 1–2.5 s pauses) and it backs off if a waiting room
  appears. Colombia runs on Shopify and uses its public JSON feed.
* **Discovery**: reads each shop's menu, finds the football sections in any language, scans them once and keeps the
  smallest set that covers every product (so a parent section replaces its children). Refreshed weekly.
* **Speed**: after the browser passes a shop's gate once, pages are fetched over plain HTTP with that session,
  several in parallel, so a full check of all shops takes a few minutes instead of ~15, and a search is a few seconds.
* **Every cycle** (default 10 min) each section is re-read and compared with the SQLite database in `data/`.
  The first scan of a section is silent (baseline). A product never seen before ⇒ 🆕; sold-out → available ⇒ 🔔;
  price lower ⇒ 💸. More than 6 new items on one shop in a cycle are sent as one digest.
* Only your chat can use the bot; messages from anyone else are ignored.

## Tuning (`config.yaml`)

`check_interval_minutes`, which alerts are on, `price_drop_min_pct`, shop list (`enabled: false` to drop one),
extra `categories` / `searches` per shop, words that exclude categories/products, `extra_football_keywords`.

Logs: `data/panini_watch.log`. Reset everything: delete the `data` folder.

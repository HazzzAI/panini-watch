"""Minimal async Telegram Bot API client (httpx)."""
from __future__ import annotations

import asyncio
import html
import logging

import httpx

log = logging.getLogger("panini.telegram")


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def esc_attr(s: object) -> str:
    return html.escape(str(s), quote=True)


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str | int | None):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(70, connect=15))
        import os
        self._base = f"{os.getenv('TELEGRAM_API_BASE', 'https://api.telegram.org')}/bot{token}/"

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, retries: int = 3, **params):
        for attempt in range(retries):
            try:
                r = await self._http.post(self._base + method, json=params)
            except httpx.HTTPError as e:
                if attempt == retries - 1:
                    raise TelegramError(f"network error: {e}") from e
                await asyncio.sleep(2 * (attempt + 1))
                continue
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429:
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                await asyncio.sleep(wait + 1)
                continue
            raise TelegramError(f"{method}: {data.get('description', r.text[:200])}")
        raise TelegramError(f"{method}: too many retries")

    # -- convenience -------------------------------------------------------------------------
    async def me(self):
        return await self.call("getMe")

    async def send(self, text: str, *, markup: dict | None = None, chat_id=None, preview: bool = False,
                   silent: bool = False):
        params = dict(chat_id=chat_id or self.chat_id, text=text[:4096], parse_mode="HTML",
                      disable_notification=silent, link_preview_options={"is_disabled": not preview})
        if markup:
            params["reply_markup"] = markup
        return await self.call("sendMessage", **params)

    async def send_photo(self, photo: str, caption: str, *, markup: dict | None = None, chat_id=None):
        params = dict(chat_id=chat_id or self.chat_id, photo=photo, caption=caption[:1024], parse_mode="HTML")
        if markup:
            params["reply_markup"] = markup
        try:
            return await self.call("sendPhoto", retries=2, **params)
        except TelegramError as e:
            log.info("photo failed (%s), falling back to text", e)
            return await self.send(caption, markup=markup, chat_id=chat_id)

    async def edit(self, chat_id, message_id, text: str, *, markup: dict | None = None):
        params = dict(chat_id=chat_id, message_id=message_id, text=text[:4096], parse_mode="HTML",
                      link_preview_options={"is_disabled": True})
        if markup is not None:
            params["reply_markup"] = markup
        try:
            return await self.call("editMessageText", retries=1, **params)
        except TelegramError as e:
            if "not modified" in str(e):
                return None
            raise

    async def answer(self, callback_id: str, text: str = "", alert: bool = False):
        try:
            await self.call("answerCallbackQuery", retries=1, callback_query_id=callback_id, text=text[:200],
                            show_alert=alert)
        except TelegramError:
            pass

    async def updates(self, offset: int | None, timeout: int = 50):
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return await self.call("getUpdates", retries=1, **params)

    async def set_commands(self, commands: list[tuple[str, str]]):
        await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])


def button(text: str, *, data: str | None = None, url: str | None = None) -> dict:
    b: dict = {"text": text[:60]}
    if url:
        b["url"] = url
    else:
        b["callback_data"] = (data or "noop")[:64]
    return b


def keyboard(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}

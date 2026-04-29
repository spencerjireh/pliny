from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Update:
    update_id: int
    message: dict[str, Any] | None


class TelegramAPIError(RuntimeError):
    """Raised when the Telegram Bot API returns ok=false."""


class TelegramClient:
    """Thin httpx wrapper around the Telegram Bot API.

    The client is intentionally minimal: long-poll `getUpdates`, send/edit
    messages, and download files. No webhook support, no parse_mode (plain
    text avoids markdown-escaping pitfalls in item titles).
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = f"{base_url}/bot{token}"
        self._file_base = f"{base_url}/file/bot{token}"
        if client is not None:
            self._client = client
            self._owns = False
        else:
            # Long-poll timeout is 30s server-side; client must exceed that.
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0))
            self._owns = True

    async def get_updates(self, offset: int, *, timeout_s: int = 30) -> list[Update]:
        resp = await self._client.get(
            f"{self._base}/getUpdates",
            params={"offset": offset, "timeout": timeout_s},
            timeout=httpx.Timeout(timeout_s + 5.0, connect=10.0),
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise TelegramAPIError(body.get("description", "getUpdates failed"))
        out: list[Update] = []
        for u in body.get("result", []):
            out.append(Update(update_id=int(u["update_id"]), message=u.get("message")))
        return out

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        resp = await self._client.post(f"{self._base}/sendMessage", json=payload)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise TelegramAPIError(body.get("description", "sendMessage failed"))
        return int(body["result"]["message_id"])

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        resp = await self._client.post(
            f"{self._base}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise TelegramAPIError(body.get("description", "editMessageText failed"))

    async def download_file(self, file_id: str) -> bytes:
        info = await self._client.get(f"{self._base}/getFile", params={"file_id": file_id})
        info.raise_for_status()
        body = info.json()
        if not body.get("ok"):
            raise TelegramAPIError(body.get("description", "getFile failed"))
        file_path = body["result"]["file_path"]
        resp = await self._client.get(f"{self._file_base}/{file_path}")
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

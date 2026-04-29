import json
from typing import Any

import httpx


class PlinyClient:
    """Thin httpx wrapper for the bot's calls into the Pliny API.

    Calls hit `/v1/items` (ingest JSON or multipart), `/v1/items/:id/status`,
    and `/v1/items/:id`. The `client` kwarg lets tests inject an
    `ASGITransport`-backed client to drive the in-process FastAPI app.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if client is not None:
            self._client = client
            self._owns = False
        else:
            self._client = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30.0,
            )
            self._owns = True

    async def ingest_json(
        self,
        *,
        text: str | None = None,
        url: str | None = None,
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"source": "telegram", "source_ref": source_ref}
        if text is not None:
            payload["text"] = text
        if url is not None:
            payload["url"] = url
        if metadata is not None:
            payload["metadata"] = metadata
        resp = await self._client.post("/v1/items", json=payload)
        resp.raise_for_status()
        return list(resp.json().get("items", []))

    async def ingest_file(
        self,
        body: bytes,
        mime: str | None,
        *,
        filename: str = "file",
        source_ref: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        files = {"file": (filename, body, mime or "application/octet-stream")}
        data: dict[str, str] = {"source": "telegram", "source_ref": source_ref}
        if metadata is not None:
            data["metadata"] = json.dumps(metadata)
        resp = await self._client.post("/v1/items", files=files, data=data)
        resp.raise_for_status()
        return list(resp.json().get("items", []))

    async def get_status(self, item_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/v1/items/{item_id}/status")
        resp.raise_for_status()
        return dict(resp.json())

    async def get_item(self, item_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/v1/items/{item_id}")
        resp.raise_for_status()
        return dict(resp.json())

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

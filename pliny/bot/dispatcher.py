from dataclasses import dataclass, field
from typing import Any

from pliny.bot.pliny_client import PlinyClient
from pliny.bot.telegram_api import TelegramClient


@dataclass(frozen=True)
class DispatchResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None


# Order matters: documents/photos may carry a caption that we ignore in favor of
# the binary payload. Voice/audio/video/animation are mutually exclusive in
# practice; iterate in a fixed order so behavior is deterministic.
_FILE_KEYS: tuple[tuple[str, str], ...] = (
    ("voice", "audio/ogg"),
    ("audio", "audio/mpeg"),
    ("video", "video/mp4"),
    ("animation", "video/mp4"),
)


async def dispatch_message(
    msg: dict[str, Any],
    *,
    telegram: TelegramClient,
    pliny: PlinyClient,
) -> DispatchResult:
    """Map one Telegram message to one ingest call.

    Text + URL splitting happens server-side in the ingest endpoint, so a text
    message with embedded URLs becomes one POST that may produce multiple items.
    """
    chat_id = msg["chat"]["id"]
    message_id = msg["message_id"]
    source_ref = f"tg:{chat_id}:{message_id}"
    metadata = _extract_metadata(msg)

    if "photo" in msg:
        largest = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        body = await telegram.download_file(largest["file_id"])
        items = await pliny.ingest_file(
            body, "image/jpeg", filename="photo.jpg", source_ref=source_ref, metadata=metadata
        )
        return DispatchResult(items=items)

    if "document" in msg:
        doc = msg["document"]
        body = await telegram.download_file(doc["file_id"])
        items = await pliny.ingest_file(
            body,
            doc.get("mime_type"),
            filename=doc.get("file_name", "file"),
            source_ref=source_ref,
            metadata=metadata,
        )
        return DispatchResult(items=items)

    for key, default_mime in _FILE_KEYS:
        if key in msg:
            obj = msg[key]
            body = await telegram.download_file(obj["file_id"])
            items = await pliny.ingest_file(
                body,
                obj.get("mime_type", default_mime),
                filename=f"{key}.bin",
                source_ref=source_ref,
                metadata=metadata,
            )
            return DispatchResult(items=items)

    text = msg.get("text") or msg.get("caption")
    if text:
        items = await pliny.ingest_json(text=text, source_ref=source_ref, metadata=metadata)
        return DispatchResult(items=items)

    return DispatchResult(note="unsupported message type")


def _extract_metadata(msg: dict[str, Any]) -> dict[str, Any]:
    md: dict[str, Any] = {
        "telegram": {
            "chat_id": msg["chat"]["id"],
            "message_id": msg["message_id"],
            "from_user_id": (msg.get("from") or {}).get("id"),
        }
    }
    if "forward_from" in msg or "forward_from_chat" in msg:
        md["forwarded_from"] = {
            "sender": msg.get("forward_from"),
            "original_chat": msg.get("forward_from_chat"),
            "forwarded_at": msg.get("forward_date"),
        }
    return md

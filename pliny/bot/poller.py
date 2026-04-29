import asyncio
import contextlib
from typing import Any

from pliny.bot.pliny_client import PlinyClient
from pliny.bot.telegram_api import TelegramClient

POLL_INTERVAL_S = 3.0
TERMINAL_SUMMARIZE = frozenset({"done", "failed"})
_TELEGRAM_TEXT_CAP = 4096


async def wait_and_render_message(
    *,
    chat_id: int,
    bot_message_id: int,
    item_ids: list[str],
    pliny: PlinyClient,
    telegram: TelegramClient,
    stop: asyncio.Event,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> None:
    """Poll status for `item_ids` until each is terminal, following redirects.
    Edit the bot's reply once with a summary."""
    tracked = list(item_ids)
    final: dict[str, dict[str, Any]] = {}

    while tracked and not stop.is_set():
        next_round: list[str] = []
        for iid in tracked:
            try:
                status = await pliny.get_status(iid)
            except Exception:
                next_round.append(iid)
                continue

            if "redirect_to" in status:
                survivor = str(status["redirect_to"])
                final.setdefault(iid, {"redirected_to": survivor})
                if survivor not in final:
                    next_round.append(survivor)
                continue

            stages = status.get("stages") or {}
            summarize = (stages.get("summarize") or {}).get("status", "pending")
            overall = status.get("overall", "pending")

            if summarize in TERMINAL_SUMMARIZE or overall == "failed":
                if summarize == "done":
                    try:
                        item = await pliny.get_item(iid)
                    except Exception:
                        next_round.append(iid)
                        continue
                    final[iid] = {
                        "status": "ready",
                        "title": item.get("title"),
                        "summary": item.get("summary"),
                    }
                else:
                    final[iid] = {"status": "failed", "error": _first_error(stages)}
            else:
                next_round.append(iid)

        tracked = next_round
        if tracked:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_s)

    text = _format_summary(item_ids, final)
    # message edit is best-effort; failures should not crash the bot loop
    with contextlib.suppress(Exception):
        await telegram.edit_message_text(chat_id=chat_id, message_id=bot_message_id, text=text)


def _first_error(stages: dict[str, Any]) -> str:
    for stage_name, info in stages.items():
        if isinstance(info, dict) and info.get("status") == "failed":
            err = info.get("error") or "failed"
            return f"{stage_name}: {err}"
    return "failed"


def _format_summary(initial_ids: list[str], final: dict[str, dict[str, Any]]) -> str:
    """Compose the bot's edited reply text.

    Single-item: title + summary, or `Capture failed: …`, or
    `Captured (already had this).` followed by the survivor's title/summary
    when the original id was redirected.
    Multi-item: a header plus per-item bullets, truncated to fit 4096 chars.
    """
    if len(initial_ids) == 1:
        iid = initial_ids[0]
        return _render_single(iid, final)

    lines: list[str] = [f"Captured {len(initial_ids)} items."]
    for iid in initial_ids:
        lines.append(_render_bullet(iid, final))
    text = "\n".join(lines)
    if len(text) > _TELEGRAM_TEXT_CAP:
        text = text[: _TELEGRAM_TEXT_CAP - 1] + "…"
    return text


def _render_single(iid: str, final: dict[str, dict[str, Any]]) -> str:
    rec = final.get(iid)
    if rec is None:
        return f"Captured. {iid} (still processing)"
    if "redirected_to" in rec:
        survivor_id = rec["redirected_to"]
        survivor_rec = final.get(survivor_id, {})
        prefix = "Captured (already had this)."
        body = _render_body(survivor_rec) or survivor_id
        return f"{prefix}\n\n{body}"
    return _render_body(rec) or "Captured."


def _render_body(rec: dict[str, Any]) -> str:
    if not rec:
        return ""
    if rec.get("status") == "failed":
        return f"Capture failed: {rec.get('error', 'unknown error')}"
    title = rec.get("title")
    summary = rec.get("summary")
    if title and summary:
        return f"{title}\n\n{summary}"
    if title:
        return str(title)
    if summary:
        return str(summary)
    return ""


def _render_bullet(iid: str, final: dict[str, dict[str, Any]]) -> str:
    rec = final.get(iid)
    if rec is None:
        return f"• {iid} (still processing)"
    if "redirected_to" in rec:
        survivor = rec["redirected_to"]
        survivor_rec = final.get(survivor, {})
        body = _render_body(survivor_rec) or survivor
        return f"• already had this — {body}"
    if rec.get("status") == "failed":
        return f"• failed: {rec.get('error', 'unknown error')}"
    title = rec.get("title") or "(no title)"
    summary = rec.get("summary") or ""
    if summary:
        return f"• {title}: {summary}"
    return f"• {title}"

from __future__ import annotations

import asyncio
import logging

from seedbox_mcp.config import Settings
from seedbox_mcp.errors import MediaMcpError
from seedbox_mcp.friend_tracking import list_tracked, save_tracked
from seedbox_mcp.runtime import build_services
from seedbox_mcp.telegram import send_message

logger = logging.getLogger("seedbox_mcp.friend_notify")


def _has_file(service: str, item: dict) -> bool:
    """Is the requested content actually in the library now?"""
    if service == "radarr":
        return bool(item.get("hasFile"))
    stats = item.get("statistics") or {}
    return int(stats.get("episodeFileCount") or 0) > 0


async def run_once() -> None:
    """Poll every tracked friend request's arr state and ping the requester on
    the two transitions they care about: approved (operator un-held it → it's
    downloading) and ready-to-watch (it's in the library). Deletes an entry
    once ready, or if the item is gone (declined/removed). Deterministic — no
    model in the loop; a friend gets told exactly what happened."""
    settings = Settings()  # type: ignore[call-arg]
    tok = settings.nasdoom_helper_telegram_bot_token
    if not tok:
        logger.warning("NASDOOM_HELPER_TELEGRAM_BOT_TOKEN unset — cannot notify friends")
        return
    token = tok.get_secret_value()

    entries = list_tracked()
    if not entries:
        return
    services = build_services(settings)
    keep: list[dict] = []
    for e in entries:
        service = e.get("service")
        arr_id = e.get("arr_id")
        chat_id = e.get("chat_id")
        title = e.get("title") or "your request"
        arr = services.radarr if service == "radarr" else services.sonarr
        if arr is None or not arr_id or not chat_id:
            continue  # misconfigured entry → drop
        path = f"/api/v3/{'movie' if service == 'radarr' else 'series'}/{arr_id}"
        try:
            item = await arr.get(path)
        except MediaMcpError as exc:
            if getattr(exc, "error_type", None) == "not_found":
                # declined or removed by the operator — stop tracking (silent;
                # telling a friend "the owner said no" isn't ours to send)
                logger.info("friend request %s/%s gone (declined/removed) — dropping", service, arr_id)
                continue
            # transient (unreachable/5xx) — keep and retry next cycle
            logger.warning("friend_notify: arr lookup failed for %s/%s: %s", service, arr_id, exc)
            keep.append(e)
            continue
        item = item if isinstance(item, dict) else {}

        if _has_file(service, item):
            await send_message(
                token, chat_id, f"*{title}* is ready to watch on Plex now. Enjoy!"
            )
            logger.info("notified %s: %s ready to watch", chat_id, title)
            continue  # done — drop

        if item.get("monitored") and not e.get("approved_notified"):
            await send_message(
                token,
                chat_id,
                f"Good news! Your request for *{title}* was approved and it's downloading now. "
                "I'll message you the moment it's ready to watch.",
            )
            logger.info("notified %s: %s approved", chat_id, title)
            e["approved_notified"] = True
        keep.append(e)

    save_tracked(keep)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(run_once())


if __name__ == "__main__":
    main()

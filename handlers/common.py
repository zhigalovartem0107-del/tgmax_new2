"""Helpers the order flows share."""

import asyncio
import logging
import math

from maxapi.types import MessageCallback

logger = logging.getLogger(__name__)

ANSWER_TIMEOUT = 10.0  # seconds to wait for MAX to take a tap's answer before going on without it


async def freeze(event: MessageCallback, notification: str | None = None) -> None:
    """Answer the tap: keep the message text, remove its buttons.

    Cosmetic, so a failing or slow MAX call is logged and the flow goes on:
    the FSM state already stops a repeated tap.
    """
    body = event.message.body if event.message else None
    try:
        async with asyncio.timeout(ANSWER_TIMEOUT):
            await event.answer(
                notification=notification,
                new_text=body.text if body else None,
                attachments=[],
                raise_if_not_exists=False,
            )
    except Exception as exc:
        logger.warning("Could not answer the tap, going on: %r", exc)


def when(seconds: float | None) -> str:
    """When to try again, for a message: "через 2 мин"."""
    if seconds is None:
        return "через пару минут"
    minutes = max(1, math.ceil(seconds / 60))
    if minutes == 1:
        return "через минуту"
    if minutes < 60:
        return f"через {minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"через {hours} ч {minutes} мин" if minutes else f"через {hours} ч"

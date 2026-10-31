"""Last router: answers button taps no other handler accepted (old messages, finished steps)."""

from maxapi import Router
from maxapi.types import MessageCallback

router = Router(router_id="fallback")


@router.message_callback()
async def on_stale_button(event: MessageCallback) -> None:
    await event.answer(notification="Кнопка устарела. Открой меню: /start")

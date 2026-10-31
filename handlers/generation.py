"""Song and video generation flow. Steps will mirror the reference bot."""

from maxapi import F, Router
from maxapi.types import MessageCallback

from keyboards import MenuAction, MenuCallback

router = Router(router_id="generation")


@router.message_callback(MenuCallback.filter(F.action.in_({MenuAction.SONG, MenuAction.VIDEO})))
async def on_generation_stub(event: MessageCallback) -> None:
    await event.answer(notification="🚧 Скоро будет доступно")

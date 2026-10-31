"""Video generation flow (not implemented yet)."""

from maxapi import F, Router
from maxapi.types import MessageCallback

from keyboards import MenuAction, MenuCallback

router = Router(router_id="video")


@router.message_callback(MenuCallback.filter(F.action == MenuAction.VIDEO))
async def on_video(event: MessageCallback) -> None:
    await event.answer(notification="🚧 Скоро будет доступно")

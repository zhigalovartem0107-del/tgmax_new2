"""Video generation: not connected yet. The button says so plainly and charges nothing."""

from maxapi import F, Router
from maxapi.types import MessageCallback

from keyboards import MenuAction, MenuCallback, main_menu_kb

router = Router(router_id="video")

IN_DEVELOPMENT = (
    "🎬 Создание видео пока в разработке — скоро появится!\n\n"
    "Деньги за видео не списываются. А пока можно создать песню 🎵"
)


@router.message_callback(MenuCallback.filter(F.action == MenuAction.VIDEO))
async def on_video(event: MessageCallback) -> None:
    # The message itself, not only a toast: MAX shows a toast briefly, or not at all.
    await event.answer(notification="🚧 В разработке", new_text=IN_DEVELOPMENT, attachments=[main_menu_kb()])

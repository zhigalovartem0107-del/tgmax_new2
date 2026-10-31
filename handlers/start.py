from maxapi import F, Router
from maxapi.filters.command import CommandStart
from maxapi.types import BotStarted, MessageCallback, MessageCreated

from database.models import User
from keyboards import MenuAction, MenuCallback, main_menu_kb

router = Router(router_id="start")


def welcome_text(user: User) -> str:
    return (
        f"Привет, {user.first_name}! 👋\n\n"
        "Я создаю песни и видео с помощью нейросетей.\n"
        "Выбери, что хочешь сделать:"
    )


HELP_TEXT = (
    "❓ Помощь\n\n"
    "/start — главное меню\n\n"
    "Раздел в разработке."
)


@router.bot_started()
async def on_bot_started(event: BotStarted, db_user: User) -> None:
    """The user pressed "Start" in a new dialog. Deep-link payload: event.payload."""
    await event.send(text=welcome_text(db_user), attachments=[main_menu_kb()])


@router.message_created(CommandStart())
async def on_start_command(event: MessageCreated, db_user: User) -> None:
    await event.message.answer(text=welcome_text(db_user), attachments=[main_menu_kb()])


@router.message_callback(MenuCallback.filter(F.action == MenuAction.HELP))
async def on_help(event: MessageCallback) -> None:
    await event.answer(new_text=HELP_TEXT)

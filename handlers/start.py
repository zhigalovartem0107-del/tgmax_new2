from maxapi import F, Router
from maxapi.context import BaseContext
from maxapi.filters.command import Command
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
    "/start — главное меню\n"
    "/cancel — отменить текущее действие"
)


@router.bot_started()
async def on_bot_started(event: BotStarted, db_user: User, context: BaseContext) -> None:
    """The user pressed "Start" in a new dialog. Deep-link payload: event.payload."""
    await context.clear()
    await event.send(text=welcome_text(db_user), attachments=[main_menu_kb()])


@router.message_created(Command(["start", "cancel"]))
async def on_start_command(event: MessageCreated, db_user: User, context: BaseContext) -> None:
    await context.clear()
    await event.message.answer(text=welcome_text(db_user), attachments=[main_menu_kb()])


@router.message_callback(MenuCallback.filter(F.action == MenuAction.MAIN))
async def on_main_menu(event: MessageCallback, db_user: User, context: BaseContext) -> None:
    await context.clear()
    await event.answer(new_text=welcome_text(db_user), attachments=[main_menu_kb()])


@router.message_callback(MenuCallback.filter(F.action == MenuAction.HELP))
async def on_help(event: MessageCallback) -> None:
    await event.answer(new_text=HELP_TEXT)

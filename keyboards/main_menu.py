from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types import CallbackButton
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


class MenuCallback(CallbackPayload, prefix="menu"):
    action: str  # see MenuAction


class MenuAction:
    MAIN = "main"
    SONG = "song"
    VIDEO = "video"
    BALANCE = "balance"
    HELP = "help"


def menu_button(text: str, action: str) -> CallbackButton:
    return CallbackButton(text=text, payload=MenuCallback(action=action).pack())


def main_menu_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(menu_button("🎵 Создать песню", MenuAction.SONG))
        .row(menu_button("🎬 Создать видео", MenuAction.VIDEO))
        .row(
            menu_button("💰 Баланс", MenuAction.BALANCE),
            menu_button("❓ Помощь", MenuAction.HELP),
        )
        .as_markup()
    )

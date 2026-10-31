from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types import CallbackButton
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


class MenuCallback(CallbackPayload, prefix="menu"):
    action: str  # see MenuAction


class MenuAction:
    SONG = "song"
    VIDEO = "video"
    BALANCE = "balance"
    HELP = "help"


def _button(text: str, action: str) -> CallbackButton:
    return CallbackButton(text=text, payload=MenuCallback(action=action).pack())


def main_menu_kb() -> AttachmentButton:
    # Placeholder layout; it will follow the reference bot's flow.
    return (
        InlineKeyboardBuilder()
        .row(_button("🎵 Создать песню", MenuAction.SONG))
        .row(_button("🎬 Создать видео", MenuAction.VIDEO))
        .row(
            _button("💰 Баланс", MenuAction.BALANCE),
            _button("❓ Помощь", MenuAction.HELP),
        )
        .as_markup()
    )

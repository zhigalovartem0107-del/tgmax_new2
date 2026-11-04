"""Video order buttons."""

from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types import CallbackButton
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from keyboards.main_menu import MenuAction, menu_button


class VideoCallback(CallbackPayload, prefix="video"):
    action: str  # see VideoAction


class VideoAction:
    NO_IMAGE = "noimg"
    CONFIRM = "confirm"


def _video_button(text: str, action: str) -> CallbackButton:
    return CallbackButton(text=text, payload=VideoCallback(action=action).pack())


def image_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(_video_button("➡️ Без картинки", VideoAction.NO_IMAGE))
        .row(menu_button("❌ Отмена", MenuAction.MAIN))
        .as_markup()
    )


def video_confirm_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(_video_button("✅ Создать видео", VideoAction.CONFIRM))
        .row(menu_button("❌ Отмена", MenuAction.MAIN))
        .as_markup()
    )

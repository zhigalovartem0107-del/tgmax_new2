from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types import CallbackButton
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from database.models import SongMode
from keyboards.main_menu import MenuAction, menu_button


class SongCallback(CallbackPayload, prefix="song"):
    action: str  # see SongAction
    value: str = ""


class SongAction:
    MODE = "mode"
    STYLE = "style"
    CONFIRM = "confirm"
    CREATE = "create"  # value: lyrics draft version
    REGENERATE = "regen"  # value: lyrics draft version


SONG_STYLES = {
    "pop": "Поп",
    "rock": "Рок",
    "rap": "Рэп",
    "electronic": "Электроника",
    "chanson": "Шансон",
    "lofi": "Лоу-фай",
}
CUSTOM_STYLE = "custom"


def _song_button(text: str, action: str, value: str = "") -> CallbackButton:
    return CallbackButton(text=text, payload=SongCallback(action=action, value=value).pack())


def _cancel_button() -> CallbackButton:
    return menu_button("❌ Отмена", MenuAction.MAIN)


def mode_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(_song_button("✍️ Свой текст", SongAction.MODE, SongMode.LYRICS))
        .row(_song_button("🤖 Текст от ИИ", SongAction.MODE, SongMode.DESCRIPTION))
        .row(_cancel_button())
        .as_markup()
    )


def style_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .add(*(_song_button(label, SongAction.STYLE, key) for key, label in SONG_STYLES.items()))
        .adjust(2)
        .row(_song_button("✏️ Свой стиль", SongAction.STYLE, CUSTOM_STYLE))
        .row(_cancel_button())
        .as_markup()
    )


def cancel_kb() -> AttachmentButton:
    return InlineKeyboardBuilder().row(_cancel_button()).as_markup()


def confirm_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(_song_button("✅ Создать", SongAction.CONFIRM))
        .row(_cancel_button())
        .as_markup()
    )


def lyrics_kb(version: int, can_regenerate: bool = True) -> AttachmentButton:
    builder = InlineKeyboardBuilder().row(
        _song_button("✅ Создать песню", SongAction.CREATE, str(version))
    )
    if can_regenerate:
        builder.row(_song_button("🔄 Перегенерировать текст", SongAction.REGENERATE, str(version)))
    return builder.row(_cancel_button()).as_markup()


def insufficient_funds_kb() -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(menu_button("💰 Пополнить", MenuAction.BALANCE))
        .row(menu_button("🏠 В меню", MenuAction.MAIN))
        .as_markup()
    )

"""Admin panel buttons."""

from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types import CallbackButton
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from services.keys import ApiKeys


class AdminCallback(CallbackPayload, prefix="adm"):
    action: str  # see AdminAction
    value: str = ""  # the key name for SET and CLEAR


class AdminAction:
    KEYS = "keys"
    SET = "set"
    CLEAR = "clear"


def _button(text: str, action: str, value: str = "") -> CallbackButton:
    return CallbackButton(text=text, payload=AdminCallback(action=action, value=value).pack())


def admin_menu_kb() -> AttachmentButton:
    return InlineKeyboardBuilder().row(_button("🔑 API Ключи", AdminAction.KEYS)).as_markup()


def keys_kb(keys: ApiKeys) -> AttachmentButton:
    """✏️ sets a key; 🗑 removes one saved in the bot (the environment variable is used again)."""
    builder = InlineKeyboardBuilder()
    for spec in keys.specs.values():
        row = [_button(f"✏️ {spec.title}", AdminAction.SET, spec.name)]
        if keys.saved_in_bot(spec.name):
            row.append(_button(f"🗑 {spec.title}", AdminAction.CLEAR, spec.name))
        builder.row(*row)
    return builder.as_markup()

from maxapi.filters.callback_payload import CallbackPayload
from maxapi.types import CallbackButton, LinkButton
from maxapi.types.attachments.buttons.attachment_button import AttachmentButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder


class ShopCallback(CallbackPayload, prefix="shop"):
    action: str  # see ShopAction
    value: str = ""


class ShopAction:
    OPEN = "open"
    PACKAGE = "pkg"
    ACCEPT = "accept"
    NO_EMAIL = "noemail"
    CHECK = "check"


def shop_button(text: str, action: str, value: str = "") -> CallbackButton:
    return CallbackButton(text=text, payload=ShopCallback(action=action, value=value).pack())


def packages_kb(packages: list[tuple[int, int]]) -> AttachmentButton:
    kb = InlineKeyboardBuilder()
    for i, (units, rubles) in enumerate(packages):
        kb.row(shop_button(f"💰 +{units} на баланс — {rubles} ₽", ShopAction.PACKAGE, str(i)))
    return kb.as_markup()


def terms_kb(terms_url: str, privacy_url: str) -> AttachmentButton:
    kb = InlineKeyboardBuilder()
    links = [LinkButton(text=t, url=u) for t, u in (("📄 Условия", terms_url), ("🔒 Политика", privacy_url)) if u]
    if links:
        kb.row(*links)
    return kb.row(shop_button("✅ Принимаю условия и политику", ShopAction.ACCEPT)).as_markup()


def email_kb() -> AttachmentButton:
    return InlineKeyboardBuilder().row(shop_button("У меня нет почты", ShopAction.NO_EMAIL)).as_markup()


def pay_kb(url: str, payment_id: int) -> AttachmentButton:
    return (
        InlineKeyboardBuilder()
        .row(LinkButton(text="Оплатить", url=url))
        .row(shop_button("🔄 Проверить оплату", ShopAction.CHECK, str(payment_id)))
        .as_markup()
    )


def finish_kb(url: str) -> AttachmentButton:
    return InlineKeyboardBuilder().row(LinkButton(text="Завершить оплату", url=url)).as_markup()

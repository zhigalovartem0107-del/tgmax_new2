"""Balance and /shop: package → terms and privacy → email for the 54-FZ receipt → YooKassa link → check.

The order lives in the database (payments table); the FSM only remembers where the user is in the dialog.
The payments worker (workers/payments.py) checks unpaid orders, reminds and cancels them.
"""

import logging
import re

from maxapi import F, Router
from maxapi.context import BaseContext, State, StatesGroup
from maxapi.filters.command import Command
from maxapi.types import MessageCallback, MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import Payment, PaymentStatus, User
from keyboards import MenuAction, MenuCallback
from keyboards.shop import ShopAction, ShopCallback, email_kb, packages_kb, pay_kb, shop_button, terms_kb
from services.yookassa import YooKassaError

logger = logging.getLogger(__name__)
router = Router(router_id="payments")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

SHOP_TEXT = "🛒 Пополнение баланса\n\nВыбери пакет:"
EMAIL_TEXT = ("📧 Пришли email — на него придёт чек об оплате (54-ФЗ).\n"
              "Нет почты — нажми кнопку ниже.")
BAD_EMAIL = "Не похоже на email. Пришли адрес вида name@mail.ru или нажми «У меня нет почты»."
NOT_CONFIGURED = "😔 Оплата пока недоступна. Попробуй позже."
CREATE_FAILED = "😔 Не удалось создать платёж. Попробуй позже: /shop"
PAID = "✅ Оплата прошла! На баланс зачислено +{units}. Баланс: {balance}."
STILL_PENDING = "⏳ Оплата ещё не завершилась. Если ты уже оплатил — подожди минуту и проверь снова."
NOT_ACTIVE = "Этот заказ уже не ждёт оплату. Новый заказ: /shop"


class ShopForm(StatesGroup):
    terms = State()
    email = State()
    pending = State()


def _terms_text() -> str:
    lines = ["📄 Перед оплатой прочитай и прими документы:"]
    if settings.terms_url:
        lines.append(f"• Условия использования: {settings.terms_url}")
    if settings.privacy_url:
        lines.append(f"• Политика конфиденциальности: {settings.privacy_url}")
    return "\n".join(lines)


def _rub(kopecks: int) -> str:
    return f"{kopecks // 100} ₽" if kopecks % 100 == 0 else f"{kopecks / 100:.2f} ₽"


@router.message_callback(MenuCallback.filter(F.action == MenuAction.BALANCE))
async def on_balance(event: MessageCallback, db_user: User) -> None:
    if db_user.has_active_subscription:
        until = db_user.subscription_expires_at.strftime("%d.%m.%Y")
        subscription = f"{db_user.subscription_plan} до {until}"
    else:
        subscription = "нет"
    await event.answer(
        new_text=f"💰 Баланс: {db_user.balance}\n📅 Подписка: {subscription}",
        attachments=[InlineKeyboardBuilder().row(shop_button("🛒 Пополнить", ShopAction.OPEN)).as_markup()],
    )


async def _show_shop(context: BaseContext, send) -> None:
    await context.clear()
    await send(text=SHOP_TEXT, attachments=[packages_kb(settings.packages)])


@router.message_created(Command("shop"))
async def on_shop_command(event: MessageCreated, context: BaseContext) -> None:
    await _show_shop(context, event.message.answer)


@router.message_callback(ShopCallback.filter(F.action == ShopAction.OPEN))
async def on_shop_button(event: MessageCallback, context: BaseContext) -> None:
    await _show_shop(context, event.send)


@router.message_callback(ShopCallback.filter(F.action == ShopAction.PACKAGE))
async def on_package(event: MessageCallback, context: BaseContext, payload: ShopCallback) -> None:
    index = int(payload.value) if payload.value.isdigit() else -1
    if not 0 <= index < len(settings.packages):
        await event.answer(notification="Пакет устарел. Открой /shop")
        return
    await context.clear()
    await context.update_data(package=index)
    await context.set_state(ShopForm.terms)
    await event.send(text=_terms_text(), attachments=[terms_kb(settings.terms_url, settings.privacy_url)])


@router.message_callback(ShopCallback.filter(F.action == ShopAction.ACCEPT), ShopForm.terms)
async def on_accept(event: MessageCallback, context: BaseContext) -> None:
    await context.set_state(ShopForm.email)
    await event.send(text=EMAIL_TEXT, attachments=[email_kb()])


@router.message_created(ShopForm.email)
async def on_email(
    event: MessageCreated, context: BaseContext, session: AsyncSession, db_user: User, yookassa
) -> None:
    body = event.message.body
    email = ((body.text or "") if body else "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        await event.message.answer(text=BAD_EMAIL, attachments=[email_kb()])
        return
    await _create(event.message.answer, event.message.recipient.chat_id, context, session, db_user, yookassa, email)


@router.message_callback(ShopCallback.filter(F.action == ShopAction.NO_EMAIL), ShopForm.email)
async def on_no_email(
    event: MessageCallback, context: BaseContext, session: AsyncSession, db_user: User, yookassa
) -> None:
    chat_id = event.message.recipient.chat_id if event.message else None
    await _create(event.send, chat_id, context, session, db_user, yookassa, None)


async def _create(send, chat_id, context, session: AsyncSession, db_user: User, yookassa, email: str | None) -> None:
    data = await context.get_data()
    units, rubles = settings.packages[data["package"]]
    if yookassa is None or not yookassa.configured:
        await context.clear()
        await send(text=NOT_CONFIGURED)
        return
    payment = await crud.create_payment(session, db_user.id, chat_id, units, rubles * 100, email)
    await session.commit()                                 # the order exists even if YooKassa is slow
    try:
        pid, url = await yookassa.create_payment(
            rubles * 100, f"Пополнение баланса +{units}", email=email, metadata={"payment": payment.id}
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("YooKassa: payment %s not created: %s", payment.id, detail)
        await crud.fail_payment(session, payment.id, detail)
        await session.commit()
        await context.clear()
        await send(text=CREATE_FAILED)
        return
    await crud.set_payment_link(session, payment.id, pid, url)
    await session.commit()
    await context.update_data(payment=payment.id)
    await context.set_state(ShopForm.pending)
    await send(
        text=f"🧾 Заказ №{payment.id}: +{units} на баланс — {_rub(rubles * 100)}.\n"
             "Нажми «Оплатить». После оплаты баланс пополнится сам (или нажми «🔄 Проверить оплату»).",
        attachments=[pay_kb(url, payment.id)],
    )


@router.message_callback(ShopCallback.filter(F.action == ShopAction.CHECK))
async def on_check(
    event: MessageCallback, context: BaseContext, session: AsyncSession, db_user: User, payload: ShopCallback, yookassa
) -> None:
    payment = await session.get(Payment, int(payload.value)) if payload.value.isdigit() else None
    if payment is None or payment.user_id != db_user.id:
        await event.answer(notification="Заказ не найден. Открой /shop")
        return
    if payment.status == PaymentStatus.SUCCEEDED:
        await event.send(text=PAID.format(units=payment.units, balance=db_user.balance))
        return
    if payment.status not in (PaymentStatus.PENDING, PaymentStatus.EXPIRED):
        await event.send(text=NOT_ACTIVE)
        return
    try:
        status = await yookassa.get_status(payment.yookassa_id)
    except Exception as exc:
        logger.error("YooKassa: status of payment %s failed: %s: %s", payment.id, type(exc).__name__, exc)
        await event.send(text="⚠️ Не получилось проверить оплату. Бот проверит сам, или попробуй через минуту.")
        return
    if status == "succeeded":
        units = await crud.complete_payment(session, payment.id)
        await session.commit()
        await session.refresh(db_user)
        await clear_shop_state(context)
        await event.send(text=PAID.format(units=units or payment.units, balance=db_user.balance))
    else:
        await event.send(text=STILL_PENDING)


async def clear_shop_state(context: BaseContext) -> None:
    """Leave the shop dialog — but never interrupt another flow the user has moved on to."""
    state = await context.get_state()
    if state is not None and str(state).startswith(f"{ShopForm.__name__}:"):
        await context.clear()

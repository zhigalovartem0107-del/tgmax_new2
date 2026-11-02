"""Background job: YooKassa orders from /shop.

Every POLL_SECONDS it asks YooKassa about unpaid orders: paid → credit the balance (exactly once) and end the
shop dialog; canceled by YooKassa → tell the user; unpaid after REMIND_AFTER → one reminder; unpaid after
CANCEL_AFTER → cancel message. An expired order is still checked for LATE_WINDOW: if the user paid in an old
tab after all, the balance is credited. Exact YooKassa errors go to the log.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from maxapi import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import crud
from database.models import Payment, PaymentStatus, User, utcnow
from handlers.payments import PAID, clear_shop_state
from keyboards.shop import finish_kb

logger = logging.getLogger(__name__)

POLL_SECONDS = 30
REMIND_AFTER = timedelta(minutes=10)
CANCEL_AFTER = timedelta(minutes=45)
LATE_WINDOW = timedelta(days=1)

REMINDER_TEXT = "⏳ Оплата ещё не завершена. Остался последний шаг — нажми «Завершить оплату»."
CANCELED_TEXT = "Оплата отменена. Баланс не изменился. Новый заказ: /shop"


async def _end_dialog(dp: Dispatcher, payment: Payment) -> None:
    ctx = dp.contexts.get((payment.chat_id, payment.user_id))
    if ctx is not None:
        await clear_shop_state(ctx)


async def _check(bot: Bot, dp: Dispatcher, yookassa, session: AsyncSession, payment: Payment, now: datetime) -> None:
    try:
        status = await yookassa.get_status(payment.yookassa_id)
    except Exception as exc:
        logger.error("YooKassa: status of payment %s failed: %s: %s", payment.id, type(exc).__name__, exc)
        return
    if status == "succeeded":
        units = await crud.complete_payment(session, payment.id)
        await session.commit()
        if units:
            user = await session.get(User, payment.user_id)
            await session.refresh(user)
            await _end_dialog(dp, payment)
            await bot.send_message(user_id=payment.user_id, text=PAID.format(units=units, balance=user.balance))
        return
    if payment.status != PaymentStatus.PENDING:
        return                                            # expired: only a late success matters
    age = now - payment.created_at
    if status == "canceled" or age >= CANCEL_AFTER:
        closed = await crud.close_payment(
            session, payment.id, PaymentStatus.CANCELED if status == "canceled" else PaymentStatus.EXPIRED
        )
        await session.commit()
        if closed:
            await _end_dialog(dp, payment)
            await bot.send_message(user_id=payment.user_id, text=CANCELED_TEXT)
    elif age >= REMIND_AFTER and not payment.reminded:
        await crud.mark_reminded(session, payment.id)
        await session.commit()
        await bot.send_message(
            user_id=payment.user_id, text=REMINDER_TEXT, attachments=[finish_kb(payment.confirmation_url)]
        )


async def process_payments(
    bot: Bot, dp: Dispatcher, yookassa, session_factory: async_sessionmaker[AsyncSession],
    now: datetime | None = None,
) -> None:
    """One pass over the open orders."""
    now = now or utcnow()
    async with session_factory() as session:
        payments = await crud.open_payments(session, LATE_WINDOW)
        for payment in payments:
            try:
                await _check(bot, dp, yookassa, session, payment, now)
            except Exception:
                await session.rollback()
                logger.exception("Payment %s: check failed, will retry", payment.id)


async def run_payment_worker(
    bot: Bot, dp: Dispatcher, yookassa, session_factory: async_sessionmaker[AsyncSession],
    interval: float = POLL_SECONDS,
) -> None:
    """Runs until cancelled; one failing pass doesn't stop it."""
    while True:
        if yookassa is not None and yookassa.configured:
            try:
                await process_payments(bot, dp, yookassa, session_factory)
            except Exception:
                logger.exception("Payment worker pass failed")
        await asyncio.sleep(interval)

"""Payment worker on a fake clock: success, reminder at 10 min, cancel at 45 min, late success, errors."""

import logging
from datetime import timedelta

from conftest import CHAT_ID, USER_ID
from database import crud, session_factory
from database.models import Payment, PaymentStatus, utcnow
from handlers.payments import ShopForm
from workers.payments import CANCELED_TEXT, REMINDER_TEXT, process_payments


async def make_payment(session, created_at) -> int:
    await crud.get_or_create_user(session, USER_ID, "Ivan")
    payment = await crud.create_payment(session, USER_ID, CHAT_ID, units=5, amount_kopecks=19900, email=None)
    await crud.set_payment_link(session, payment.id, "pay-1", "https://yoomoney.ru/checkout/pay-1")
    payment.created_at = created_at
    await session.commit()
    return payment.id


async def run(bot, now):
    await process_payments(bot.bot, bot.dp, bot.yookassa, session_factory, now=now)


async def test_success_credits_once_and_clears_shop_state(bot, session):
    t0 = utcnow()
    pid = await make_payment(session, t0)
    bot.yookassa.status["pay-1"] = "pending"
    ctx = bot.dp.contexts.setdefault((CHAT_ID, USER_ID), bot.dp.storage(CHAT_ID, USER_ID))
    await ctx.set_state(ShopForm.pending)
    await run(bot, t0 + timedelta(minutes=2))
    assert not bot.sent
    bot.yookassa.status["pay-1"] = "succeeded"
    await run(bot, t0 + timedelta(minutes=3))
    await run(bot, t0 + timedelta(minutes=4))
    user = await crud.get_user(session, USER_ID)
    assert user.balance == 5 and len([s for s in bot.sent if "Оплата прошла" in (s.text or "")]) == 1
    assert await ctx.get_state() is None
    assert (await session.get(Payment, pid)).status == PaymentStatus.SUCCEEDED


async def test_reminder_at_10_minutes_once(bot, session):
    t0 = utcnow()
    await make_payment(session, t0)
    await run(bot, t0 + timedelta(minutes=9))
    assert not bot.sent
    await run(bot, t0 + timedelta(minutes=10))
    await run(bot, t0 + timedelta(minutes=11))
    reminders = [s for s in bot.sent if s.text == REMINDER_TEXT]
    assert len(reminders) == 1 and reminders[0].user_id == USER_ID
    assert reminders[0].buttons["Завершить оплату"] == "https://yoomoney.ru/checkout/pay-1"


async def test_cancel_at_45_minutes_then_late_success(bot, session):
    t0 = utcnow()
    pid = await make_payment(session, t0)
    ctx = bot.dp.contexts.setdefault((CHAT_ID, USER_ID), bot.dp.storage(CHAT_ID, USER_ID))
    await ctx.set_state(ShopForm.pending)
    await run(bot, t0 + timedelta(minutes=45))
    assert bot.sent[-1].text == CANCELED_TEXT and await ctx.get_state() is None
    assert (await session.get(Payment, pid)).status == PaymentStatus.EXPIRED
    assert (await crud.get_user(session, USER_ID)).balance == 0
    bot.yookassa.status["pay-1"] = "succeeded"                         # paid in an old tab after all
    await run(bot, t0 + timedelta(minutes=50))
    assert (await crud.get_user(session, USER_ID)).balance == 5


async def test_cancel_does_not_touch_other_flows(bot, session):
    from handlers.song import SongForm
    t0 = utcnow()
    await make_payment(session, t0)
    ctx = bot.dp.contexts.setdefault((CHAT_ID, USER_ID), bot.dp.storage(CHAT_ID, USER_ID))
    await ctx.set_state(SongForm.text)                                 # the user moved on to a song
    await run(bot, t0 + timedelta(minutes=45))
    assert await ctx.get_state() == SongForm.text.name or str(await ctx.get_state()) == str(SongForm.text)


async def test_yookassa_canceled(bot, session):
    t0 = utcnow()
    pid = await make_payment(session, t0)
    bot.yookassa.status["pay-1"] = "canceled"
    await run(bot, t0 + timedelta(minutes=1))
    assert bot.sent[-1].text == CANCELED_TEXT
    assert (await session.get(Payment, pid)).status == PaymentStatus.CANCELED


async def test_status_error_is_logged_and_retried(bot, session, caplog):
    t0 = utcnow()
    pid = await make_payment(session, t0)
    from services.yookassa import YooKassaError
    bot.yookassa.status_fail = YooKassaError(500, '{"code": "internal_server_error", "id": "e-77"}')
    with caplog.at_level(logging.ERROR):
        await run(bot, t0 + timedelta(minutes=1))
    assert "e-77" in caplog.text and (await session.get(Payment, pid)).status == PaymentStatus.PENDING
    bot.yookassa.status_fail = None
    bot.yookassa.status["pay-1"] = "succeeded"
    await run(bot, t0 + timedelta(minutes=2))
    assert (await crud.get_user(session, USER_ID)).balance == 5

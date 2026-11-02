"""/shop: package → terms and privacy → email for the receipt → YooKassa link → check payment."""

import logging

from sqlalchemy import select

from config import settings
from conftest import CHAT_ID, USER_ID
from database import crud
from database.models import Payment, PaymentStatus
from services.yookassa import YooKassaError

ACCEPT = "✅ Принимаю условия и политику"
NO_EMAIL = "У меня нет почты"
CHECK = "🔄 Проверить оплату"


async def state_of(bot):
    ctx = bot.dp.contexts.get((CHAT_ID, USER_ID))
    return await ctx.get_state() if ctx else None


async def open_shop(bot):
    await bot.start()
    await bot.text("/shop")
    pkg = next(t for t in bot.last.buttons if "199" in t)
    await bot.tap(pkg)


async def test_full_flow_with_email(bot, session):
    await open_shop(bot)
    terms = bot.last
    assert settings.terms_url in terms.text and settings.privacy_url in terms.text
    assert terms.buttons["📄 Условия"] == settings.terms_url and terms.buttons["🔒 Политика"] == settings.privacy_url
    await bot.tap(ACCEPT)
    assert "почт" in bot.last.text and NO_EMAIL in bot.last.buttons
    await bot.text("ivan@@example")
    assert "не похоже" in bot.last.text.lower() and not bot.yookassa.created   # stays on the email step
    await bot.text("Ivan@Example.com ")
    created = bot.yookassa.created[-1]
    assert created["email"] == "ivan@example.com" and created["amount_kopecks"] == 19900
    pay = bot.last
    assert pay.buttons["Оплатить"] == "https://yoomoney.ru/checkout/pay-1" and CHECK in pay.buttons
    await bot.tap(CHECK)
    assert "ещё не" in bot.last.text
    bot.yookassa.status["pay-1"] = "succeeded"
    await bot.tap(CHECK, message=pay)
    assert "Оплата прошла" in bot.last.text
    user = await crud.get_user(session, USER_ID)
    assert user.balance == 5 and await state_of(bot) is None
    await bot.tap(CHECK, message=pay)                                     # pressed again: no second top-up
    await session.refresh(user)
    assert user.balance == 5


async def test_without_email(bot):
    await open_shop(bot)
    await bot.tap(ACCEPT)
    await bot.tap(NO_EMAIL)
    assert bot.yookassa.created[-1]["email"] is None and "Оплатить" in bot.last.buttons


async def test_balance_screen_leads_to_shop(bot):
    await bot.start()
    await bot.tap("💰 Баланс")
    await bot.tap("🛒 Пополнить")
    assert any("199" in t for t in bot.last.buttons)


async def test_yookassa_error_is_logged_and_flow_ends(bot, session, caplog):
    bot.yookassa.fail = YooKassaError(401, '{"id": "019a-err", "code": "invalid_credentials"}')
    await open_shop(bot)
    await bot.tap(ACCEPT)
    with caplog.at_level(logging.ERROR):
        await bot.text("ivan@example.com")
    assert "Не удалось создать платёж" in bot.last.text and await state_of(bot) is None
    assert "019a-err" in caplog.text and "invalid_credentials" in caplog.text
    payment = (await session.scalars(select(Payment))).first()
    assert payment.status == PaymentStatus.FAILED and "019a-err" in payment.error

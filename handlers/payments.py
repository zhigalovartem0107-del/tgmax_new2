"""Balance, top-ups and subscriptions. YooKassa flow comes next."""

from maxapi import F, Router
from maxapi.types import MessageCallback

from database.models import User
from keyboards import MenuAction, MenuCallback

router = Router(router_id="payments")


@router.message_callback(MenuCallback.filter(F.action == MenuAction.BALANCE))
async def on_balance(event: MessageCallback, db_user: User) -> None:
    if db_user.has_active_subscription:
        until = db_user.subscription_expires_at.strftime("%d.%m.%Y")
        subscription = f"{db_user.subscription_plan} до {until}"
    else:
        subscription = "нет"
    await event.answer(
        new_text=f"💰 Баланс: {db_user.balance}\n📅 Подписка: {subscription}",
    )

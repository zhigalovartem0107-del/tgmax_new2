"""Admin commands, allowed only for the MAX user ids listed in ADMIN_IDS.

They are not in the public "/" menu. Anyone else gets "no access" with their
own ID, which is how the owner finds the ID to put into ADMIN_IDS.
"""

from maxapi import Router
from maxapi.filters.command import Command
from maxapi.filters.filter import BaseFilter
from maxapi.types import MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import GenerationStatus, User

router = Router(router_id="admin")

TOP_UP_MAX = 1_000_000
USAGE = "Использование: /add_balance <сумма>, например /add_balance 100"


class IsAdmin(BaseFilter):
    async def __call__(self, event: MessageCreated) -> bool:
        sender = event.message.sender
        return sender is not None and sender.user_id in settings.admin_ids


def _parse_amount(args: list[str]) -> int | None:
    """A whole amount from 1 to TOP_UP_MAX, or None."""
    if len(args) != 1:
        return None
    try:
        amount = int(args[0])
    except ValueError:
        return None
    return amount if 0 < amount <= TOP_UP_MAX else None


@router.message_created(Command("admin"), IsAdmin())
async def on_admin(event: MessageCreated, session: AsyncSession) -> None:
    stats = await crud.get_stats(session)
    count = stats.generations_by_status
    in_progress = count.get(GenerationStatus.PENDING, 0) + count.get(GenerationStatus.PROCESSING, 0)
    await event.message.answer(
        text=(
            "📊 Статистика\n\n"
            f"👥 Пользователи: {stats.users} (новых за сутки: {stats.new_users_24h})\n"
            f"🎵 Генерации: {sum(count.values())} — в работе {in_progress}, "
            f"готово {count.get(GenerationStatus.DONE, 0)}, "
            f"ошибок {count.get(GenerationStatus.FAILED, 0)}\n"
            f"💰 Списано: {stats.spent} · на балансах: {stats.balances}\n\n"
            "/add_balance <сумма> — пополнить свой баланс"
        )
    )


@router.message_created(Command(["add_balance", "add_credits"]), IsAdmin())
async def on_add_balance(
    event: MessageCreated, session: AsyncSession, db_user: User, args: list[str]
) -> None:
    amount = _parse_amount(args)
    if amount is None:
        await event.message.answer(text=USAGE)
        return
    balance = await crud.add_balance(session, db_user.id, amount)
    await event.message.answer(text=f"✅ Баланс пополнен на {amount}. Теперь: {balance}")


@router.message_created(Command(["admin", "add_balance", "add_credits"]))
async def on_admin_refused(event: MessageCreated) -> None:
    sender = event.message.sender
    user_id = sender.user_id if sender else "—"
    await event.message.answer(text=f"⛔ Нет доступа (ваш ID: {user_id}).")

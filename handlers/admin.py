"""Admin commands and panel, allowed only for the MAX user ids listed in ADMIN_IDS.

They are not in the public "/" menu. Anyone else gets "no access" with their
own ID, which is how the owner finds the ID to put into ADMIN_IDS.
"""

import logging

from maxapi import F, Router
from maxapi.context import BaseContext, State, StatesGroup
from maxapi.filters.command import Command
from maxapi.filters.filter import BaseFilter
from maxapi.types import MessageCallback, MessageCreated
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database import crud
from database.models import GenerationStatus, User
from keyboards.admin import AdminAction, AdminCallback, admin_menu_kb, keys_kb
from services.keys import ApiKeys, clean, mask

logger = logging.getLogger(__name__)
router = Router(router_id="admin")

TOP_UP_MAX = 1_000_000
USAGE = "Использование: /add_balance <сумма>, например /add_balance 100"


class AdminForm(StatesGroup):
    key_value = State()  # waiting for the new value of the key in the FSM data


class IsAdmin(BaseFilter):
    """Messages and button taps from users listed in ADMIN_IDS."""

    async def __call__(self, event) -> bool:
        if isinstance(event, MessageCallback):
            user = event.callback.user
        elif isinstance(event, MessageCreated):
            user = event.message.sender
        else:
            return False
        return user is not None and user.user_id in settings.admin_ids


def _parse_amount(args: list[str]) -> int | None:
    """A whole amount from 1 to TOP_UP_MAX, or None."""
    if len(args) != 1:
        return None
    try:
        amount = int(args[0])
    except ValueError:
        return None
    return amount if 0 < amount <= TOP_UP_MAX else None


def _keys_text(keys: ApiKeys) -> str:
    lines = ["🔑 API ключи", ""]
    for spec in keys.specs.values():
        value = keys.get(spec.name)
        if value is None:
            status = "не задан"
        elif keys.saved_in_bot(spec.name):
            status = f"{mask(value)} — сохранён в боте"
        else:
            status = f"{mask(value)} — из переменной окружения"
        lines.append(f"• {spec.title} ({spec.name}): {status}")
    lines += ["", "Ключ из бота важнее переменной окружения и работает сразу, без перезапуска."]
    return "\n".join(lines)


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
        ),
        attachments=[admin_menu_kb()],
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


@router.message_callback(AdminCallback.filter(F.action == AdminAction.KEYS), IsAdmin())
async def on_keys(event: MessageCallback, context: BaseContext, keys: ApiKeys) -> None:
    await context.clear()
    await event.answer(new_text=_keys_text(keys), attachments=[keys_kb(keys)])


@router.message_callback(AdminCallback.filter(F.action == AdminAction.SET), IsAdmin())
async def on_set_key(
    event: MessageCallback, context: BaseContext, keys: ApiKeys, payload: AdminCallback
) -> None:
    if payload.value not in keys.specs:
        await event.answer(notification="Неизвестный ключ")
        return
    await context.set_state(AdminForm.key_value)
    await context.update_data(key=payload.value)  # the name only; the value never goes into the FSM
    await event.answer(
        new_text=(
            f"✏️ Пришли новый {payload.value} одним сообщением — "
            "я сохраню его и удалю сообщение из чата.\n/cancel — отмена."
        ),
        attachments=[],
    )


@router.message_callback(AdminCallback.filter(F.action == AdminAction.CLEAR), IsAdmin())
async def on_clear_key(event: MessageCallback, keys: ApiKeys, payload: AdminCallback) -> None:
    if payload.value not in keys.specs:
        await event.answer(notification="Неизвестный ключ")
        return
    await keys.clear(payload.value)
    await event.answer(
        notification=f"{payload.value} удалён из бота",
        new_text=_keys_text(keys),
        attachments=[keys_kb(keys)],
    )


@router.message_created(AdminForm.key_value, IsAdmin())
async def on_key_value(event: MessageCreated, context: BaseContext, keys: ApiKeys, db_user: User) -> None:
    name = (await context.get_data()).get("key")
    if name not in keys.specs:
        await context.clear()
        return
    body = event.message.body
    value = clean(body.text or "") if body else ""
    if not value or value.startswith("/"):  # a command typed instead of the key isn't saved
        await event.message.answer(text=f"Пришли {name} текстом. /cancel — отмена.")
        return

    await keys.set(name, value, admin_id=db_user.id)
    await context.clear()
    try:  # the key shouldn't stay in the chat history
        deleted = (await event.message.delete()).success
    except Exception:
        deleted = False
    warning = ""
    if not deleted:
        logger.warning("Could not delete the admin's message with %s", name)
        warning = "\n⚠️ Не получилось удалить сообщение с ключом — удали его сам."
    await event.message.answer(
        text=f"✅ {name} сохранён: {mask(value)}. Уже используется.{warning}\n\n{_keys_text(keys)}",
        attachments=[keys_kb(keys)],
    )


@router.message_created(Command(["admin", "add_balance", "add_credits"]))
async def on_admin_refused(event: MessageCreated) -> None:
    sender = event.message.sender
    user_id = sender.user_id if sender else "—"
    await event.message.answer(text=f"⛔ Нет доступа (ваш ID: {user_id}).")


@router.message_callback(AdminCallback.filter())
async def on_admin_button_refused(event: MessageCallback) -> None:
    await event.answer(notification="⛔ Нет доступа")

"""Database operations. Handlers call these instead of building queries themselves."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from database.models import Generation, GenerationStatus, Payment, PaymentStatus, User, utcnow


@dataclass(frozen=True)
class Stats:
    users: int
    new_users_24h: int
    generations_by_status: dict[str, int]
    spent: int  # cost of generations that were not refunded
    balances: int  # sum of all users' balances


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_or_create_user(
    session: AsyncSession,
    user_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
) -> tuple[User, bool]:
    """Return (user, created). Keeps the stored profile fields in sync with MAX."""
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, first_name=first_name, last_name=last_name, username=username)
        session.add(user)
        await session.flush()
        return user, True

    if (user.first_name, user.last_name, user.username) != (first_name, last_name, username):
        user.first_name, user.last_name, user.username = first_name, last_name, username
    return user, False


async def add_balance(session: AsyncSession, user_id: int, amount: int) -> int:
    """Credit the balance atomically and return the new value."""
    if amount <= 0:
        raise ValueError("amount must be positive")
    result = await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(balance=User.balance + amount)
        .returning(User.balance)
    )
    return result.scalar_one()


async def try_charge_balance(session: AsyncSession, user_id: int, amount: int) -> int | None:
    """Debit the balance atomically. Returns the new balance, or None if funds are insufficient.

    The check and the debit are one UPDATE statement, so two concurrent
    generation requests can never spend the same credits twice.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    result = await session.execute(
        update(User)
        .where(User.id == user_id, User.balance >= amount)
        .values(balance=User.balance - amount)
        .returning(User.balance)
    )
    return result.scalar_one_or_none()


async def start_paid_generation(
    session: AsyncSession,
    user_id: int,
    kind: str,
    prompt: str,
    cost: int,
    mode: str | None = None,
    style: str | None = None,
    lyrics: str | None = None,
) -> Generation | None:
    """Charge `cost` and record the job in one transaction. None if funds are insufficient."""
    if await try_charge_balance(session, user_id, cost) is None:
        return None
    generation = Generation(
        user_id=user_id,
        kind=kind,
        mode=mode,
        style=style,
        prompt=prompt,
        lyrics=lyrics,
        cost=cost,
    )
    session.add(generation)
    await session.flush()
    return generation


async def fail_generation(session: AsyncSession, generation: Generation, error: str) -> bool:
    """Mark the job as failed and refund its cost — exactly once. The handler and the delivery worker may try
    at the same time, each with its own session, so the check, the status flip and the refund are one guarded
    UPDATE, not a check of the in-memory object. False if it was already finished (failed or delivered)."""
    row = (
        await session.execute(
            update(Generation)
            .where(Generation.id == generation.id,
                   Generation.status.in_([GenerationStatus.PENDING, GenerationStatus.PROCESSING]))
            .values(status=GenerationStatus.FAILED, error=error[:1000])
            .returning(Generation.user_id, Generation.cost)
        )
    ).first()
    if row is None:
        return False
    set_committed_value(generation, "status", GenerationStatus.FAILED)
    set_committed_value(generation, "error", error[:1000])
    await add_balance(session, row.user_id, row.cost)
    return True


async def mark_processing(session: AsyncSession, generation_id: int, task_id: str) -> bool:
    """Pending -> processing with the provider's task id. False if the job was refunded meanwhile (the worker
    gave up on a slow submit): then the late result must be ignored, not delivered for free."""
    result = await session.execute(
        update(Generation)
        .where(Generation.id == generation_id, Generation.status == GenerationStatus.PENDING)
        .values(status=GenerationStatus.PROCESSING, provider_task_id=task_id)
    )
    return result.rowcount == 1


async def get_stats(session: AsyncSession) -> Stats:
    users = await session.scalar(select(func.count()).select_from(User))
    new_users = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.created_at >= utcnow() - timedelta(days=1))
    )
    rows = await session.execute(
        select(Generation.status, func.count()).group_by(Generation.status)
    )
    spent = await session.scalar(
        select(func.coalesce(func.sum(Generation.cost), 0)).where(
            Generation.status != GenerationStatus.FAILED
        )
    )
    balances = await session.scalar(select(func.coalesce(func.sum(User.balance), 0)))
    return Stats(
        users=users,
        new_users_24h=new_users,
        generations_by_status=dict(rows.all()),
        spent=spent,
        balances=balances,
    )


# --- Payments (/shop) ---

async def create_payment(
    session: AsyncSession, user_id: int, chat_id: int | None, units: int, amount_kopecks: int, email: str | None
) -> Payment:
    payment = Payment(user_id=user_id, chat_id=chat_id, units=units, amount_kopecks=amount_kopecks, email=email)
    session.add(payment)
    await session.flush()
    return payment


async def set_payment_link(session: AsyncSession, payment_id: int, yookassa_id: str, url: str) -> None:
    await session.execute(
        update(Payment).where(Payment.id == payment_id).values(yookassa_id=yookassa_id, confirmation_url=url)
    )


async def fail_payment(session: AsyncSession, payment_id: int, error: str) -> None:
    await session.execute(
        update(Payment).where(Payment.id == payment_id).values(status=PaymentStatus.FAILED, error=error[:2000])
    )


async def complete_payment(session: AsyncSession, payment_id: int) -> int | None:
    """Mark paid and credit the balance exactly once. Returns the credited units, or None if it was
    already completed (a second check or the worker got there first)."""
    row = (
        await session.execute(
            update(Payment)
            .where(Payment.id == payment_id,
                   Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.EXPIRED]))
            .values(status=PaymentStatus.SUCCEEDED)
            .returning(Payment.user_id, Payment.units)
        )
    ).first()
    if row is None:
        return None
    await add_balance(session, row.user_id, row.units)
    return row.units


async def close_payment(session: AsyncSession, payment_id: int, status: PaymentStatus) -> bool:
    """Pending -> canceled/expired. False if it is no longer pending."""
    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status == PaymentStatus.PENDING)
        .values(status=status)
    )
    return result.rowcount == 1


async def mark_reminded(session: AsyncSession, payment_id: int) -> None:
    await session.execute(update(Payment).where(Payment.id == payment_id).values(reminded=True))


async def open_payments(session: AsyncSession, late_window: timedelta) -> list[Payment]:
    """Payments still worth checking: pending ones, and expired ones younger than late_window."""
    since = utcnow() - late_window
    return list(
        (
            await session.scalars(
                select(Payment).where(
                    Payment.yookassa_id.is_not(None),
                    (Payment.status == PaymentStatus.PENDING)
                    | ((Payment.status == PaymentStatus.EXPIRED) & (Payment.created_at >= since)),
                )
            )
        ).all()
    )

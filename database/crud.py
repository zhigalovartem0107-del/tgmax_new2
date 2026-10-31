"""Database operations. Handlers call these instead of building queries themselves."""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Generation, GenerationStatus, User


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


async def fail_generation(session: AsyncSession, generation: Generation, error: str) -> None:
    """Mark the job as failed and refund its cost. Safe to call more than once."""
    if generation.status == GenerationStatus.FAILED:
        return
    generation.status = GenerationStatus.FAILED
    generation.error = error[:1000]
    await add_balance(session, generation.user_id, generation.cost)

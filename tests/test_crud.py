from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from database import GenerationKind, GenerationStatus, User, crud
from database.models import utcnow


async def test_get_or_create_user_syncs_profile(session):
    user, created = await crud.get_or_create_user(session, 1, "Ann", None, "ann")
    assert created and user.balance == 0
    await session.commit()

    user, created = await crud.get_or_create_user(session, 1, "Anna", "K", "ann")
    assert not created and (user.first_name, user.last_name) == ("Anna", "K")
    assert user.created_at.tzinfo is not None


async def test_balance_is_atomic_and_never_negative(session):
    await crud.get_or_create_user(session, 1, "Ann")
    assert await crud.add_balance(session, 1, 100) == 100
    assert await crud.try_charge_balance(session, 1, 30) == 70
    assert await crud.try_charge_balance(session, 1, 71) is None

    with pytest.raises(IntegrityError):
        await session.execute(update(User).where(User.id == 1).values(balance=-1))


async def test_subscription_survives_sqlite_roundtrip(session):
    user, _ = await crud.get_or_create_user(session, 1, "Ann")
    user.subscription_plan = "pro"
    user.subscription_expires_at = utcnow() + timedelta(days=30)
    await session.commit()
    session.expunge_all()

    user = await crud.get_user(session, 1)
    assert user.has_active_subscription


async def test_paid_generation_charges_and_refunds_once(session):
    await crud.get_or_create_user(session, 1, "Ann")
    await crud.add_balance(session, 1, 10)

    assert await crud.start_paid_generation(session, 1, GenerationKind.SONG, "p", cost=11) is None

    generation = await crud.start_paid_generation(session, 1, GenerationKind.SONG, "p", cost=10)
    assert generation.status == GenerationStatus.PENDING
    assert (await crud.get_user(session, 1)).balance == 0

    await crud.fail_generation(session, generation, "boom")
    await crud.fail_generation(session, generation, "boom")
    assert generation.status == GenerationStatus.FAILED
    assert (await crud.get_user(session, 1)).balance == 10


async def test_refund_happens_once_even_from_two_sessions(session):
    """The handler and the delivery worker may both refund the same order (each with its own session):
    the money must come back only once."""
    from database import session_factory
    from database.models import Generation, GenerationKind, GenerationStatus

    await crud.get_or_create_user(session, 1, "A")
    await crud.add_balance(session, 1, 10)
    gen = await crud.start_paid_generation(session, 1, GenerationKind.SONG, prompt="p", cost=10)
    await session.commit()
    async with session_factory() as s1, session_factory() as s2:
        g1, g2 = await s1.get(Generation, gen.id), await s2.get(Generation, gen.id)
        await crud.fail_generation(s1, g1, "worker: timed out")
        await s1.commit()
        await crud.fail_generation(s2, g2, "handler: provider error")
        await s2.commit()
    await session.refresh(await crud.get_user(session, 1))
    assert (await crud.get_user(session, 1)).balance == 10
    await session.refresh(gen)
    assert gen.status == GenerationStatus.FAILED


async def test_refunded_order_is_not_revived_by_a_late_submit(session):
    """The worker refunded an order the music server took too long to accept; the late answer must not
    flip it back to processing (that would deliver a song that was already refunded)."""
    from database.models import GenerationKind, GenerationStatus

    await crud.get_or_create_user(session, 2, "B")
    await crud.add_balance(session, 2, 10)
    gen = await crud.start_paid_generation(session, 2, GenerationKind.SONG, prompt="p", cost=10)
    await session.commit()
    await crud.fail_generation(session, gen, "never reached the music server")
    await session.commit()
    assert await crud.mark_processing(session, gen.id, "task-1") is False
    await session.refresh(gen)
    assert gen.status == GenerationStatus.FAILED and gen.provider_task_id is None

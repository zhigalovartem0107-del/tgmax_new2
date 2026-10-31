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

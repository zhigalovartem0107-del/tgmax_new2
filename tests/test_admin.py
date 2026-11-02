from datetime import timedelta

import pytest
from conftest import USER_ID

from config import Settings, settings
from database import GenerationKind, GenerationStatus, User, crud
from database.models import utcnow


@pytest.fixture
def admin(monkeypatch):
    """Makes the test user (USER_ID) an admin, as ADMIN_IDS=777 would."""
    monkeypatch.setattr(settings, "admin_ids", [USER_ID])


async def _balance(session, user_id=USER_ID):
    session.expunge_all()
    return (await crud.get_user(session, user_id)).balance


def test_admin_ids_come_from_env(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "777, 888")
    assert Settings(_env_file=None).admin_ids == [777, 888]


async def test_regular_user_is_refused(bot, session):
    await bot.start()

    await bot.text("/add_balance 100")
    assert "777" in bot.last.text  # tells the user their ID for ADMIN_IDS
    await bot.text("/admin")
    assert "777" in bot.last.text and "Пользователи" not in bot.last.text

    assert await _balance(session) == 0


async def test_add_balance_tops_up_admins_own_balance(bot, session, admin):
    await bot.start()

    await bot.text("/add_balance 150")
    assert "150" in bot.last.text
    assert await _balance(session) == 150

    await bot.text("/add_credits 50")
    assert "200" in bot.last.text
    assert await _balance(session) == 200


@pytest.mark.parametrize(
    "command",
    [
        "/add_balance",
        "/add_balance abc",
        "/add_balance 0",
        "/add_balance -5",
        "/add_balance 1000000000000",
    ],
)
async def test_add_balance_rejects_invalid_amounts(bot, session, admin, command):
    await bot.start()

    await bot.text(command)

    assert "/add_balance" in bot.last.text  # usage hint
    assert await _balance(session) == 0


async def test_admin_shows_stats(bot, session, admin):
    await bot.start()  # the admin: 0 on balance
    session.add(User(id=1003, first_name="Old", created_at=utcnow() - timedelta(days=2)))
    await crud.get_or_create_user(session, 1001, "Anna")
    await crud.get_or_create_user(session, 1002, "Boris")
    await crud.add_balance(session, 1001, 30)
    await crud.add_balance(session, 1002, 10)
    in_progress = await crud.start_paid_generation(session, 1001, GenerationKind.SONG, "p", cost=10)
    in_progress.status = GenerationStatus.PROCESSING
    refunded = await crud.start_paid_generation(session, 1002, GenerationKind.SONG, "p", cost=10)
    await crud.fail_generation(session, refunded, "provider down")
    await session.commit()

    await bot.text("/admin")

    stats = bot.last.text
    assert "Пользователи: 4" in stats and "за сутки: 3" in stats
    assert "Генерации: 2" in stats and "в работе 1" in stats and "ошибок 1" in stats
    assert "Списано: 10" in stats  # the refunded order doesn't count
    assert "на балансах: 30" in stats  # 1001: 20, 1002: 10 (refunded), admin and 1003: 0


async def test_admin_command_works_mid_order(bot, admin):
    await bot.start()
    await bot.tap("🎵 Создать песню")
    await bot.tap("🤖 Текст от ИИ")
    await bot.tap("Рок")  # now the bot expects the song description

    await bot.text("/admin")

    assert "Пользователи: 1" in bot.last.text

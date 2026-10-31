import pytest
from conftest import USER_ID
from sqlalchemy import select

from database import Generation, GenerationStatus, crud
from handlers.song import LYRICS_MAX, STYLE_MAX
from services import MusicService

PRICE = 10  # SONG_PRICE in conftest


@pytest.fixture
def provider(monkeypatch):
    calls = []

    async def generate_song(self, prompt, style, custom_lyrics):
        calls.append({"prompt": prompt, "style": style, "custom_lyrics": custom_lyrics})
        return "task-1"

    monkeypatch.setattr(MusicService, "generate_song", generate_song)
    return calls


async def _fund(session, amount):
    await crud.add_balance(session, USER_ID, amount)
    await session.commit()


async def _generations(session):
    session.expunge_all()
    return (await session.scalars(select(Generation))).all()


async def _balance(session):
    session.expunge_all()
    return (await crud.get_user(session, USER_ID)).balance


async def _order_ai_rock_song(bot):
    await bot.tap("🎵 Создать песню")
    await bot.tap("🤖 Текст от ИИ")
    await bot.tap("Рок")
    assert bot.last.text.startswith("💡 Опиши, о чём песня")
    await bot.text("песня про кота")


async def test_full_flow_charges_and_submits(bot, session, provider):
    await bot.start()
    await _fund(session, 25)

    await _order_ai_rock_song(bot)
    summary = bot.last.text
    assert "Режим: текст от ИИ" in summary and "Стиль: Рок" in summary
    assert "Описание: песня про кота" in summary and f"Стоимость: {PRICE} (на балансе: 25)" in summary

    await bot.tap("✅ Создать")
    assert bot.last.text.startswith("🎶 Песня создаётся")
    assert provider == [{"prompt": "песня про кота", "style": "Рок", "custom_lyrics": False}]
    assert await _balance(session) == 15

    [generation] = await _generations(session)
    assert generation.status == GenerationStatus.PROCESSING
    assert (generation.provider_task_id, generation.mode, generation.cost) == ("task-1", "description", PRICE)


async def test_insufficient_funds_charges_nothing(bot, session, provider):
    await bot.start()
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")

    assert bot.last.text == f"😔 Недостаточно средств: нужно {PRICE}, на балансе 0."
    assert set(bot.last.buttons) == {"💰 Пополнить", "🏠 В меню"}
    assert provider == [] and await _generations(session) == []


async def test_provider_failure_refunds(bot, session):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")  # MusicService is not configured -> NotImplementedError

    assert "Средства вернулись на баланс" in bot.last.text
    assert await _balance(session) == PRICE
    [generation] = await _generations(session)
    assert generation.status == GenerationStatus.FAILED and "NotImplementedError" in generation.error


async def test_double_tap_on_confirm_charges_once(bot, session, provider):
    await bot.start()
    await _fund(session, 100)
    await _order_ai_rock_song(bot)
    confirm = bot.last

    await bot.tap("✅ Создать", confirm)
    await bot.tap("✅ Создать", confirm)

    assert bot.last.notification == "Кнопка устарела. Открой меню: /start"
    assert len(provider) == 1 and await _balance(session) == 100 - PRICE


async def test_custom_style_own_lyrics_and_validation(bot, provider):
    await bot.start()
    await bot.tap("🎵 Создать песню")
    await bot.tap("✍️ Свой текст")
    await bot.tap("✏️ Свой стиль")

    await bot.text("x" * (STYLE_MAX + 1))
    assert bot.last.text == f"Пришли стиль текстом, до {STYLE_MAX} символов."

    await bot.text("инди-рок, женский вокал")
    assert bot.last.text.startswith("✍️ Пришли текст песни")

    await bot.text("л" * (LYRICS_MAX + 1))
    assert bot.last.text.startswith(f"Слишком длинно: {LYRICS_MAX + 1} символов")

    await bot.text("Куплет\nПрипев")
    assert "Стиль: инди-рок, женский вокал" in bot.last.text
    assert "Текст: Куплет\nПрипев" in bot.last.text


async def test_cancel_resets_flow(bot):
    await bot.start()
    await bot.tap("🎵 Создать песню")
    await bot.tap("🤖 Текст от ИИ")
    await bot.tap("❌ Отмена")
    assert bot.last.text.startswith("Привет, Ivan!")

    await bot.tap("🎵 Создать песню", bot.sent[0])
    await bot.tap("✍️ Свой текст")
    await bot.tap("Поп")
    await bot.text("/cancel")
    assert bot.last.text.startswith("Привет, Ivan!")

    count = len(bot.sent)
    await bot.text("это уже не текст песни")
    assert len(bot.sent) == count  # state cleared, message ignored


async def test_menu_buttons_work_mid_flow(bot):
    await bot.start()
    menu = bot.last
    await bot.tap("🎵 Создать песню", menu)
    await bot.tap("🤖 Текст от ИИ")

    await bot.tap("💰 Баланс", menu)
    assert bot.last.text.startswith("💰 Баланс: 0")

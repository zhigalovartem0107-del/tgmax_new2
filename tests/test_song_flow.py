from types import SimpleNamespace

import pytest
from conftest import USER_ID
from sqlalchemy import select

from database import Generation, GenerationStatus, crud
from handlers.song import LYRICS_DRAFTS_MAX, LYRICS_MAX, STYLE_MAX
from services import MusicService
from services.ai import GeminiService, LyricsError

PRICE = 10  # SONG_PRICE in conftest
LYRICS_BUTTONS = {"✅ Создать песню", "🔄 Перегенерировать текст", "❌ Отмена"}


def lyrics_message(lyrics: str) -> str:
    return f"📝 Текст песни:\n\n{lyrics}\n\n💰 Создание песни: {PRICE}"


@pytest.fixture(autouse=True)
def gemini(monkeypatch):
    """Fake Gemini: returns queued drafts in order ("Текст N" by default); queued errors are raised."""
    fake = SimpleNamespace(calls=[], drafts=[])

    async def write_lyrics(self, description, style):
        fake.calls.append((description, style))
        draft = fake.drafts.pop(0) if fake.drafts else f"Текст {len(fake.calls)}"
        if isinstance(draft, Exception):
            raise draft
        return draft

    monkeypatch.setattr(GeminiService, "write_lyrics", write_lyrics)
    return fake


@pytest.fixture
def provider(monkeypatch):
    calls = []

    async def generate_song(self, lyrics, style):
        calls.append({"lyrics": lyrics, "style": style})
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


async def test_ai_lyrics_are_written_and_shown(bot, session, gemini):
    await bot.start()
    await _fund(session, 25)
    await _order_ai_rock_song(bot)
    summary = bot.last.text
    assert "Режим: текст от ИИ" in summary and "Стиль: Рок" in summary
    assert "Описание: песня про кота" in summary and f"Стоимость: {PRICE} (на балансе: 25)" in summary

    await bot.tap("✅ Создать")

    assert gemini.calls == [("песня про кота", "Рок")]
    frozen = bot.sent[-2]
    assert frozen.notification == "✍️ Пишу текст песни…" and frozen.buttons == {}
    assert bot.last.text == lyrics_message("Текст 1")
    assert set(bot.last.buttons) == LYRICS_BUTTONS
    assert await _balance(session) == 25 and await _generations(session) == []


async def test_create_song_uses_latest_draft(bot, session, gemini, provider):
    await bot.start()
    await _fund(session, 25)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    first_draft = bot.last

    await bot.tap("🔄 Перегенерировать текст")
    assert bot.sent[-2].notification == "✍️ Пишу новый вариант…"
    latest_draft = bot.last
    assert latest_draft.text == lyrics_message("Текст 2")

    await bot.tap("✅ Создать песню", first_draft)  # outdated draft
    assert bot.last.notification == "Используй кнопки под последним текстом."
    assert provider == []

    await bot.tap("✅ Создать песню", latest_draft)
    assert bot.last.text.startswith("🎶 Песня создаётся")
    assert provider == [{"lyrics": "Текст 2", "style": "Рок"}]
    assert await _balance(session) == 25 - PRICE

    [generation] = await _generations(session)
    assert (generation.status, generation.provider_task_id) == (GenerationStatus.PROCESSING, "task-1")
    assert (generation.mode, generation.prompt, generation.lyrics) == (
        "description",
        "песня про кота",
        "Текст 2",
    )


async def test_regenerations_are_capped(bot, session, gemini):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    for _ in range(LYRICS_DRAFTS_MAX - 1):
        await bot.tap("🔄 Перегенерировать текст")

    assert bot.last.text == lyrics_message(f"Текст {LYRICS_DRAFTS_MAX}")
    assert set(bot.last.buttons) == {"✅ Создать песню", "❌ Отмена"}
    assert len(gemini.calls) == LYRICS_DRAFTS_MAX


@pytest.mark.parametrize(
    "bad_draft",
    [LyricsError("Gemini API error 503: overloaded"), "л" * (LYRICS_MAX + 1)],
    ids=["api-error", "too-long"],
)
async def test_failed_draft_can_be_retried(bot, session, gemini, bad_draft):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    gemini.drafts.append(bad_draft)

    await bot.tap("✅ Создать")
    assert bot.last.text == "😔 Не получилось написать текст. Попробуй ещё раз."
    assert set(bot.last.buttons) == {"✅ Создать", "❌ Отмена"}

    await bot.tap("✅ Создать")
    assert bot.last.text == lyrics_message("Текст 2")
    assert await _balance(session) == PRICE


async def test_failed_redraft_keeps_previous_lyrics(bot, session, gemini, provider):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    gemini.drafts.append(LyricsError("timeout"))

    await bot.tap("🔄 Перегенерировать текст")
    assert bot.last.text.startswith("😔 Не получилось написать новый текст")
    assert set(bot.last.buttons) == LYRICS_BUTTONS

    await bot.tap("✅ Создать песню")
    assert provider == [{"lyrics": "Текст 1", "style": "Рок"}]


async def test_own_lyrics_skip_gemini(bot, session, gemini, provider):
    await bot.start()
    await _fund(session, 25)
    await bot.tap("🎵 Создать песню")
    await bot.tap("✍️ Свой текст")
    await bot.tap("Поп")
    await bot.text("Куплет\nПрипев")
    await bot.tap("✅ Создать")

    assert gemini.calls == []
    assert bot.last.text.startswith("🎶 Песня создаётся")
    assert provider == [{"lyrics": "Куплет\nПрипев", "style": "Поп"}]
    assert await _balance(session) == 25 - PRICE
    [generation] = await _generations(session)
    assert (generation.mode, generation.lyrics) == ("lyrics", "Куплет\nПрипев")


async def test_insufficient_funds_skips_gemini_and_charges_nothing(bot, session, gemini, provider):
    await bot.start()
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")

    assert bot.last.text == f"😔 Недостаточно средств: нужно {PRICE}, на балансе 0."
    assert set(bot.last.buttons) == {"💰 Пополнить", "🏠 В меню"}
    assert gemini.calls == [] and provider == [] and await _generations(session) == []


async def test_provider_failure_refunds(bot, session, gemini):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    await bot.tap("✅ Создать песню")  # MusicService is not configured -> NotImplementedError

    assert "Средства вернулись на баланс" in bot.last.text
    assert await _balance(session) == PRICE
    [generation] = await _generations(session)
    assert generation.status == GenerationStatus.FAILED and "NotImplementedError" in generation.error


async def test_double_tap_on_create_song_charges_once(bot, session, gemini, provider):
    await bot.start()
    await _fund(session, 100)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    draft = bot.last

    await bot.tap("✅ Создать песню", draft)
    await bot.tap("✅ Создать песню", draft)

    assert bot.last.notification == "Кнопка устарела. Открой меню: /start"
    assert len(provider) == 1 and await _balance(session) == 100 - PRICE


async def test_custom_style_own_lyrics_and_validation(bot):
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

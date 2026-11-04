import asyncio
from types import SimpleNamespace

import pytest
from conftest import USER_ID
from maxapi import Bot
from maxapi.exceptions.max import MaxApiError
from sqlalchemy import select

from config import Settings, settings
from database import Generation, GenerationStatus, crud
from handlers import common
from handlers import song as song_handlers
from handlers.song import LYRICS_MAX, STYLE_MAX
from services import MusicService
from services.ai import AIService, LyricsError, LyricsRateLimited

PRICE = 10  # SONG_PRICE in conftest
LYRICS_BUTTONS = {"✅ Создать песню", "🔄 Перегенерировать текст", "❌ Отмена"}


def lyrics_message(lyrics: str) -> str:
    return f"📝 Текст песни:\n\n{lyrics}\n\n💰 Создание песни: {PRICE}"


@pytest.fixture(autouse=True)
def ai(monkeypatch):
    """Fake AI: returns queued drafts in order ("Текст N" by default); queued errors are raised."""
    fake = SimpleNamespace(calls=[], drafts=[])

    async def write_lyrics(self, description, style):
        fake.calls.append((description, style))
        draft = fake.drafts.pop(0) if fake.drafts else f"Текст {len(fake.calls)}"
        if isinstance(draft, Exception):
            raise draft
        return draft

    monkeypatch.setattr(AIService, "write_lyrics", write_lyrics)
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


async def test_ai_lyrics_are_written_and_shown(bot, session, ai):
    await bot.start()
    await _fund(session, 25)
    await _order_ai_rock_song(bot)
    summary = bot.last.text
    assert "Режим: текст от ИИ" in summary and "Стиль: Рок" in summary
    assert "Описание: песня про кота" in summary and f"Стоимость: {PRICE} (на балансе: 25)" in summary

    await bot.tap("✅ Создать")

    assert ai.calls == [("песня про кота", "Рок")]
    frozen = bot.sent[-2]
    assert frozen.notification == "✍️ Пишу текст песни…" and frozen.buttons == {}
    assert bot.last.text == lyrics_message("Текст 1")
    assert set(bot.last.buttons) == LYRICS_BUTTONS
    assert await _balance(session) == 25 and await _generations(session) == []


async def test_create_song_uses_latest_draft(bot, session, ai, provider):
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


async def test_regenerations_are_capped(bot, session, ai):
    cap = settings.lyrics_drafts_max
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    for _ in range(cap - 1):
        await bot.tap("🔄 Перегенерировать текст")

    assert bot.last.text.startswith(lyrics_message(f"Текст {cap}"))
    assert "последний вариант" in bot.last.text  # says why there is no 🔄 any more
    assert set(bot.last.buttons) == {"✅ Создать песню", "❌ Отмена"}
    assert len(ai.calls) == cap


def test_drafts_per_order_come_from_env(monkeypatch):
    monkeypatch.delenv("LYRICS_DRAFTS_MAX", raising=False)
    assert Settings(_env_file=None).lyrics_drafts_max == 10
    monkeypatch.setenv("LYRICS_DRAFTS_MAX", "0")
    assert Settings(_env_file=None).lyrics_drafts_max == 0


@pytest.mark.parametrize("cap", [3, 0], ids=["three", "no-limit"])
async def test_drafts_per_order_are_configurable(bot, session, ai, monkeypatch, cap):
    monkeypatch.setattr(settings, "lyrics_drafts_max", cap)
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    for _ in range(2):
        await bot.tap("🔄 Перегенерировать текст")

    assert len(ai.calls) == 3
    if cap:
        assert set(bot.last.buttons) == {"✅ Создать песню", "❌ Отмена"}
        assert "последний вариант" in bot.last.text
    else:
        assert set(bot.last.buttons) == LYRICS_BUTTONS
        assert bot.last.text == lyrics_message("Текст 3")


@pytest.mark.parametrize(
    ("retry_after", "when"),
    [
        (600, "через 10 мин"),
        (90, "через 2 мин"),
        (20, "через минуту"),
        (3 * 3600 + 60, "через 3 ч 1 мин"),
        (None, "через пару минут"),
    ],
    ids=["10-min", "2-min", "1-min", "3-h", "unknown"],
)
async def test_rate_limit_says_when_to_retry(bot, session, ai, retry_after, when):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    ai.drafts.append(LyricsRateLimited(retry_after))

    await bot.tap("✅ Создать")
    assert bot.last.text.startswith("⏳") and when in bot.last.text
    assert set(bot.last.buttons) == {"✅ Создать", "❌ Отмена"}  # the order is kept for the retry

    await bot.tap("✅ Создать")
    assert bot.last.text == lyrics_message("Текст 2")


async def test_rate_limited_redraft_keeps_previous_lyrics(bot, session, ai, provider):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    ai.drafts.append(LyricsRateLimited(60))

    await bot.tap("🔄 Перегенерировать текст")
    assert bot.last.text.startswith("⏳") and "через минуту" in bot.last.text
    assert set(bot.last.buttons) == LYRICS_BUTTONS

    await bot.tap("✅ Создать песню")
    assert provider == [{"lyrics": "Текст 1", "style": "Рок"}]


@pytest.mark.parametrize(
    "bad_draft",
    [LyricsError("OpenRouter API error 503: overloaded"), "л" * (LYRICS_MAX + 1)],
    ids=["api-error", "too-long"],
)
async def test_failed_draft_can_be_retried(bot, session, ai, bad_draft):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    ai.drafts.append(bad_draft)

    await bot.tap("✅ Создать")
    assert bot.last.text == "😔 Не получилось написать текст. Попробуй ещё раз."
    assert set(bot.last.buttons) == {"✅ Создать", "❌ Отмена"}

    await bot.tap("✅ Создать")
    assert bot.last.text == lyrics_message("Текст 2")
    assert await _balance(session) == PRICE


async def test_failed_redraft_keeps_previous_lyrics(bot, session, ai, provider):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    ai.drafts.append(LyricsError("timeout"))

    await bot.tap("🔄 Перегенерировать текст")
    assert bot.last.text.startswith("😔 Не получилось написать новый текст")
    assert set(bot.last.buttons) == LYRICS_BUTTONS

    await bot.tap("✅ Создать песню")
    assert provider == [{"lyrics": "Текст 1", "style": "Рок"}]


async def test_own_lyrics_skip_ai(bot, session, ai, provider):
    await bot.start()
    await _fund(session, 25)
    await bot.tap("🎵 Создать песню")
    await bot.tap("✍️ Свой текст")
    await bot.tap("Поп")
    await bot.text("Куплет\nПрипев")
    await bot.tap("✅ Создать")

    assert ai.calls == []
    assert bot.last.text.startswith("🎶 Песня создаётся")
    assert provider == [{"lyrics": "Куплет\nПрипев", "style": "Поп"}]
    assert await _balance(session) == 25 - PRICE
    [generation] = await _generations(session)
    assert (generation.mode, generation.lyrics) == ("lyrics", "Куплет\nПрипев")


async def test_insufficient_funds_skips_ai_and_charges_nothing(bot, session, ai, provider):
    await bot.start()
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")

    assert bot.last.text == f"😔 Недостаточно средств: нужно {PRICE}, на балансе 0."
    assert set(bot.last.buttons) == {"💰 Пополнить", "🏠 В меню"}
    assert ai.calls == [] and provider == [] and await _generations(session) == []


async def test_provider_failure_refunds(bot, session, ai):
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")
    await bot.tap("✅ Создать песню")  # no MUSIC_API_KEY in tests -> MusicError

    assert "Средства вернулись на баланс" in bot.last.text
    assert await _balance(session) == PRICE
    [generation] = await _generations(session)
    assert generation.status == GenerationStatus.FAILED and "MUSIC_API_KEY" in generation.error


async def test_double_tap_on_create_song_charges_once(bot, session, ai, provider):
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


# --- MAX trouble with cosmetic calls ("typing…", answering a tap) never costs the user the lyrics ---


async def _until(condition, timeout: float = 6.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def _never_answers(*args, **kwargs):
    await asyncio.Event().wait()


async def test_failed_typing_indicator_does_not_skip_the_lyrics(bot, session, ai, monkeypatch):
    """MAX refusing "typing…" used to end the draft before the AI was even asked."""

    async def send_action(self, chat_id=None, action=None, *a, **kw):
        raise MaxApiError(code=500, raw="typing failed")

    monkeypatch.setattr(Bot, "send_action", send_action)
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)

    await bot.tap("✅ Создать")

    assert ai.calls == [("песня про кота", "Рок")]
    assert bot.last.text == lyrics_message("Текст 1")


async def test_typing_failure_while_the_ai_works_keeps_the_lyrics(bot, session, monkeypatch):
    """A "typing…" refresh failing mid-draft used to throw away the lyrics the AI had written."""
    monkeypatch.setattr(song_handlers, "TYPING_INTERVAL", 0.01, raising=False)
    actions = []

    async def send_action(self, chat_id=None, action=None, *a, **kw):
        actions.append(action)
        if len(actions) > 1:
            raise MaxApiError(code=500, raw="typing failed")

    async def write_lyrics(self, description, style):
        await _until(lambda: len(actions) > 1)  # a refresh has failed by the time the AI answers
        return "Текст про кота"

    monkeypatch.setattr(Bot, "send_action", send_action)
    monkeypatch.setattr(AIService, "write_lyrics", write_lyrics)
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)

    await bot.tap("✅ Создать")

    assert bot.last.text == lyrics_message("Текст про кота")


async def test_hanging_typing_indicator_does_not_hold_up_the_lyrics(bot, session, ai, monkeypatch):
    monkeypatch.setattr(Bot, "send_action", _never_answers)
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)

    await asyncio.wait_for(bot.tap("✅ Создать"), timeout=3)

    assert bot.last.text == lyrics_message("Текст 1")


@pytest.mark.parametrize("trouble", ["fails", "hangs"])
async def test_tap_answer_trouble_does_not_skip_the_lyrics(bot, session, ai, monkeypatch, trouble):
    """Removing the tapped buttons is cosmetic: the draft goes on if MAX fails or is slow to do it."""
    monkeypatch.setattr(common, "ANSWER_TIMEOUT", 0.05)
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)

    async def send_callback(self, *a, **kw):
        if trouble == "hangs":
            await asyncio.Event().wait()
        raise MaxApiError(code=500, raw="callback failed")

    monkeypatch.setattr(Bot, "send_callback", send_callback)
    await asyncio.wait_for(bot.tap("✅ Создать"), timeout=3)

    assert ai.calls == [("песня про кота", "Рок")]
    assert bot.last.text == lyrics_message("Текст 1")


async def test_failed_tap_answer_still_creates_the_song(bot, session, ai, provider, monkeypatch):
    """The order used to be lost: its state was cleared, then answering the tap failed."""
    await bot.start()
    await _fund(session, PRICE)
    await _order_ai_rock_song(bot)
    await bot.tap("✅ Создать")

    async def send_callback(self, *a, **kw):
        raise MaxApiError(code=500, raw="callback failed")

    monkeypatch.setattr(Bot, "send_callback", send_callback)
    await bot.tap("✅ Создать песню")

    assert provider == [{"lyrics": "Текст 1", "style": "Рок"}]
    assert bot.last.text.startswith("🎶 Песня создаётся")
    assert await _balance(session) == 0

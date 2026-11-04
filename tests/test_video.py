"""Video order flow: description → optional first frame (a photo) → confirm → charge → Agnes AI."""

from types import SimpleNamespace

import pytest
from conftest import USER_ID
from sqlalchemy import select

from config import Settings, settings
from database import Generation, GenerationKind, GenerationStatus, crud
from services.video import VideoError, VideoRateLimited, VideoService

PRICE = 10  # SONG_PRICE in conftest; VIDEO_PRICE is unset there
PROMPT = "кот идёт по пляжу на закате"
PHOTO_URL = "https://i.oneme.ru/i?r=photo1"
CONFIRM_BUTTONS = {"✅ Создать видео", "❌ Отмена"}


@pytest.fixture
def agnes(monkeypatch):
    """Fake Agnes: records orders and answers video_N; queued errors are raised."""
    fake = SimpleNamespace(orders=[], errors=[])

    async def generate_video(self, prompt, image_url=None):
        fake.orders.append((prompt, image_url))
        if fake.errors:
            raise fake.errors.pop(0)
        return f"video_{len(fake.orders)}"

    monkeypatch.setattr(VideoService, "generate_video", generate_video)
    return fake


@pytest.fixture
async def agnes_key(bot):
    await bot.keys.set("AGNES_API_KEY", "agnes-key")


async def _fund(session, amount):
    await crud.add_balance(session, USER_ID, amount)
    await session.commit()


async def _balance(session):
    session.expunge_all()
    return (await crud.get_user(session, USER_ID)).balance


async def _videos(session):
    session.expunge_all()
    return (await session.scalars(select(Generation))).all()


async def _describe(bot, prompt=PROMPT):
    await bot.tap("🎬 Создать видео")
    assert bot.last.text.startswith("🎬 Опиши видео")
    await bot.text(prompt)
    assert bot.last.text.startswith("🖼")
    assert set(bot.last.buttons) == {"➡️ Без картинки", "❌ Отмена"}


async def test_without_agnes_key_video_is_in_development_and_free(bot, session, agnes):
    await bot.start()
    await _fund(session, 25)

    await bot.tap("🎬 Создать видео")

    assert "в разработке" in (bot.last.text or "")  # a message, not a toast that is easy to miss
    assert "не списываются" in bot.last.text
    assert "🎵 Создать песню" in bot.last.buttons
    assert agnes.orders == [] and await _balance(session) == 25 and await _videos(session) == []


async def test_without_agnes_key_a_song_order_in_progress_is_kept(bot):
    await bot.start()
    menu = bot.last
    await bot.tap("🎵 Создать песню", menu)
    await bot.tap("🤖 Текст от ИИ")
    await bot.tap("Рок")

    await bot.tap("🎬 Создать видео", menu)
    await bot.text("песня про кота")
    assert bot.last.text.startswith("🎵 Проверь заказ")


async def test_text_to_video_is_charged_and_ordered(bot, session, agnes, agnes_key):
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)

    await bot.tap("➡️ Без картинки")
    summary = bot.last.text
    assert summary.startswith("🎬 Проверь заказ")
    assert f"Описание: {PROMPT}" in summary and "Картинка: нет" in summary
    assert f"Стоимость: {PRICE} (на балансе: 25)" in summary
    assert set(bot.last.buttons) == CONFIRM_BUTTONS

    await bot.tap("✅ Создать видео")

    assert agnes.orders == [(PROMPT, None)]
    assert bot.last.text.startswith("🎬 Видео создаётся")
    assert await _balance(session) == 25 - PRICE
    [video] = await _videos(session)
    assert (video.kind, video.status, video.provider_task_id) == (
        GenerationKind.VIDEO,
        GenerationStatus.PROCESSING,
        "video_1",
    )
    assert (video.prompt, video.cost, video.mode) == (PROMPT, PRICE, "text")


async def test_photo_becomes_the_first_frame(bot, session, agnes, agnes_key):
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)

    await bot.photo(PHOTO_URL)
    assert "Картинка: твоё фото" in bot.last.text
    await bot.tap("✅ Создать видео")

    assert agnes.orders == [(PROMPT, PHOTO_URL)]
    [video] = await _videos(session)
    assert video.mode == "image"


async def test_text_instead_of_a_photo_asks_again(bot, session, agnes, agnes_key):
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)

    await bot.text("вот фото")
    assert "Пришли фото" in bot.last.text
    assert set(bot.last.buttons) == {"➡️ Без картинки", "❌ Отмена"}

    await bot.photo(PHOTO_URL)
    assert bot.last.text.startswith("🎬 Проверь заказ")


@pytest.mark.parametrize(("prompt", "reply"), [("   ", "Пришли описание"), ("к" * 1001, "Слишком длинно")])
async def test_description_is_checked(bot, agnes, agnes_key, prompt, reply):
    await bot.start()
    await bot.tap("🎬 Создать видео")

    await bot.text(prompt)
    assert reply in bot.last.text

    await bot.text(PROMPT)  # still waiting for the description
    assert bot.last.text.startswith("🖼")


async def test_insufficient_funds_charge_and_order_nothing(bot, session, agnes, agnes_key):
    await bot.start()
    await _describe(bot)
    await bot.tap("➡️ Без картинки")

    await bot.tap("✅ Создать видео")

    assert bot.last.text == f"😔 Недостаточно средств: нужно {PRICE}, на балансе 0."
    assert set(bot.last.buttons) == {"💰 Пополнить", "🏠 В меню"}
    assert agnes.orders == [] and await _videos(session) == []


async def test_rate_limited_order_is_refunded_and_can_be_retried(bot, session, agnes, agnes_key):
    """Agnes takes 1 video a minute on the free plan."""
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)
    await bot.tap("➡️ Без картинки")
    agnes.errors.append(VideoRateLimited(40))

    await bot.tap("✅ Создать видео")
    assert bot.last.text.startswith("⏳") and "через минуту" in bot.last.text
    assert "Средства вернулись на баланс" in bot.last.text
    assert set(bot.last.buttons) == CONFIRM_BUTTONS  # the order is kept for the retry
    assert await _balance(session) == 25

    await bot.tap("✅ Создать видео")
    assert bot.last.text.startswith("🎬 Видео создаётся")
    assert len(agnes.orders) == 2 and await _balance(session) == 25 - PRICE


async def test_known_wait_is_said_before_charging(bot, session, agnes, agnes_key, monkeypatch):
    monkeypatch.setattr(VideoService, "wait_before_next", lambda self: 150.0)
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)
    await bot.tap("➡️ Без картинки")

    await bot.tap("✅ Создать видео")

    assert bot.last.text.startswith("⏳") and "через 3 мин" in bot.last.text
    assert set(bot.last.buttons) == CONFIRM_BUTTONS
    assert agnes.orders == [] and await _videos(session) == []  # nothing charged, nothing to refund


async def test_failed_order_is_refunded(bot, session, agnes, agnes_key):
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)
    await bot.tap("➡️ Без картинки")
    agnes.errors.append(VideoError("Agnes HTTP 500: oops"))

    await bot.tap("✅ Создать видео")

    assert bot.last.text == "😔 Не удалось запустить создание видео. Средства вернулись на баланс."
    assert await _balance(session) == 25
    [video] = await _videos(session)
    assert video.status == GenerationStatus.FAILED and "500" in video.error


async def test_double_tap_charges_once(bot, session, agnes, agnes_key):
    await bot.start()
    await _fund(session, 100)
    await _describe(bot)
    await bot.tap("➡️ Без картинки")
    summary = bot.last

    await bot.tap("✅ Создать видео", summary)
    await bot.tap("✅ Создать видео", summary)

    assert bot.last.notification == "Кнопка устарела. Открой меню: /start"
    assert len(agnes.orders) == 1 and await _balance(session) == 100 - PRICE


async def test_cancel_ends_the_order(bot, session, agnes, agnes_key):
    await bot.start()
    await _describe(bot)

    await bot.tap("❌ Отмена")
    assert bot.last.text.startswith("Привет, Ivan!")
    count = len(bot.sent)
    await bot.photo(PHOTO_URL)
    assert len(bot.sent) == count and agnes.orders == []


async def test_video_has_its_own_price(bot, session, agnes, agnes_key, monkeypatch):
    monkeypatch.setattr(settings, "video_price", 7)
    await bot.start()
    await _fund(session, 25)
    await _describe(bot)
    await bot.tap("➡️ Без картинки")
    assert "Стоимость: 7 (на балансе: 25)" in bot.last.text

    await bot.tap("✅ Создать видео")
    assert await _balance(session) == 25 - 7


def test_video_price_defaults_to_the_song_price(monkeypatch):
    monkeypatch.delenv("VIDEO_PRICE", raising=False)
    monkeypatch.setenv("SONG_PRICE", "12")
    assert Settings(_env_file=None).video_cost == 12
    monkeypatch.setenv("VIDEO_PRICE", "30")
    assert Settings(_env_file=None).video_cost == 30

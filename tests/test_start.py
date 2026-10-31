from conftest import USER_ID

from database import crud

MENU = {"🎵 Создать песню", "🎬 Создать видео", "💰 Баланс", "❓ Помощь"}


async def test_start_button_registers_user_and_shows_menu(bot, session):
    await bot.start()
    assert bot.last.text.startswith("Привет, Ivan!")
    assert set(bot.last.buttons) == MENU
    assert await crud.get_user(session, USER_ID) is not None


async def test_start_command_shows_menu(bot):
    await bot.text("/start")
    assert set(bot.last.buttons) == MENU


async def test_balance_and_video_stub(bot):
    await bot.start()
    menu = bot.last
    await bot.tap("💰 Баланс", menu)
    assert bot.last.text == "💰 Баланс: 0\n📅 Подписка: нет"
    await bot.tap("🎬 Создать видео", menu)
    assert bot.last.notification == "🚧 Скоро будет доступно"

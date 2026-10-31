from conftest import USER_ID
from maxapi import Bot
from maxapi.connection.base import BaseConnection
from maxapi.enums.http_method import HTTPMethod
from maxapi.methods.types.setted_commands import SettedCommands

import main
from database import crud
from handlers.start import BOT_COMMANDS, HELP_TEXT

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


async def test_sync_commands_replaces_menu_on_max(monkeypatch):
    requests = []

    async def request(self, method, path, model=None, **kwargs):
        requests.append((method, path, kwargs.get("json")))
        return SettedCommands(commands=BOT_COMMANDS)

    monkeypatch.setattr(BaseConnection, "request", request)
    await main.sync_commands(Bot(token="test-token"))

    assert requests == [
        (
            HTTPMethod.PATCH,
            "/me/commands",
            {
                "commands": [
                    {"name": "start", "description": "Главное меню"},
                    {"name": "help", "description": "Помощь"},
                    {"name": "cancel", "description": "Отменить текущее действие"},
                ]
            },
        )
    ]


async def test_sync_commands_failure_does_not_stop_the_bot(monkeypatch, caplog):
    async def request(self, *args, **kwargs):
        raise RuntimeError("MAX is down")

    monkeypatch.setattr(BaseConnection, "request", request)
    await main.sync_commands(Bot(token="test-token"))  # must not raise
    assert "Could not update the bot command menu" in caplog.text


async def test_every_menu_command_answers(bot):
    for command in BOT_COMMANDS:
        count = len(bot.sent)
        await bot.text(f"/{command.name}")
        assert len(bot.sent) == count + 1, f"/{command.name} got no reply"


async def test_help_command_keeps_the_order_in_progress(bot):
    await bot.start()
    await bot.tap("🎵 Создать песню")
    await bot.tap("🤖 Текст от ИИ")
    await bot.tap("Рок")

    await bot.text("/help")
    assert bot.last.text == HELP_TEXT
    assert set(bot.last.buttons) == MENU

    await bot.text("песня про кота")
    assert bot.last.text.startswith("🎵 Проверь заказ")

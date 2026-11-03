"""The bot: dispatcher, services, command menu, polling. Started by main.py."""

import asyncio
import contextlib
import logging
import signal

from maxapi import Bot, Dispatcher
from maxapi.context import SimpleEventIsolation

from config import settings
from database import close_db, init_db, session_factory
from handlers import BOT_COMMANDS, get_routers
from middlewares import DbSessionMiddleware, ServicesMiddleware, UserMiddleware
from services import MusicService
from services.ai import AIService
from services.keys import ApiKeys
from services.yookassa import YooKassaService
from workers.payments import run_payment_worker
from workers.song_delivery import run_song_delivery

logger = logging.getLogger(__name__)


def build_dispatcher(
    ai: AIService,
    music: MusicService,
    yookassa: YooKassaService | None = None,
    keys: ApiKeys | None = None,
) -> Dispatcher:
    # Updates from different users run concurrently (an AI call takes seconds);
    # updates from one user stay in order, so their FSM steps never race.
    dp = Dispatcher(use_create_task=True, event_isolation=SimpleEventIsolation())
    # Inner middlewares run in registration order, only for updates some handler accepts.
    dp.register_inner_middleware(DbSessionMiddleware(session_factory))
    dp.register_inner_middleware(UserMiddleware())
    dp.register_inner_middleware(ServicesMiddleware(music=music, ai=ai, yookassa=yookassa, keys=keys))
    dp.include_routers(*get_routers())
    return dp


async def sync_commands(bot: Bot) -> None:
    """Replace the bot's "/" menu in MAX. The list is stored on MAX's side until replaced,
    so without this it keeps showing whatever an earlier bot version set."""
    try:
        result = await bot.set_commands(*BOT_COMMANDS)
    except Exception:
        logger.exception("Could not update the bot command menu")
        return
    names = ", ".join(f"/{command.name}" for command in result.commands or [])
    logger.info("Bot command menu on MAX: %s", names or "(empty)")


def install_stop_signals(dp: Dispatcher) -> set[asyncio.Task]:
    """Stop polling cleanly on Ctrl+C or when the host sends SIGTERM."""
    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task] = set()

    def request_stop() -> None:
        logger.info("Stop signal received, shutting down")
        tasks.add(loop.create_task(dp.stop_polling()))

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # not supported on Windows
            loop.add_signal_handler(sig, request_stop)
    return tasks


async def main() -> None:
    logging.getLogger().setLevel(settings.log_level.upper())
    ai = AIService.from_settings(settings)
    await init_db()
    keys = ApiKeys(session_factory, settings)  # keys saved in /admin win over the environment
    await keys.load()
    music = MusicService.from_settings(settings, keys=keys)
    if not keys.get("ACEMUSIC_API_KEY"):
        logger.warning("ACEMUSIC_API_KEY is not set (env or /admin → 🔑 API Ключи): songs will fail and be refunded")

    bot = Bot(token=settings.bot_token.get_secret_value())
    await sync_commands(bot)
    yookassa = YooKassaService.from_settings(settings)
    if not yookassa.configured:
        logger.warning("YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY are not set: /shop can't take payments")
    dp = build_dispatcher(ai, music, yookassa, keys)
    stop_tasks = install_stop_signals(dp)
    delivery = asyncio.create_task(run_song_delivery(bot, music, session_factory))
    payments = asyncio.create_task(run_payment_worker(bot, dp, yookassa, session_factory))

    logger.info("Bot is starting")
    try:
        await dp.start_polling(bot)
    finally:
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        for task in (delivery, payments):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await yookassa.close()
        await bot.close_session()
        await ai.close()
        await music.close()
        await close_db()
        logger.info("Bot stopped")

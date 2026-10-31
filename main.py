"""Entry point: python main.py"""

import asyncio
import contextlib
import logging
import signal

from maxapi import Bot, Dispatcher
from maxapi.context import SimpleEventIsolation

from config import settings
from database import close_db, init_db, session_factory
from handlers import get_routers
from middlewares import DbSessionMiddleware, ServicesMiddleware, UserMiddleware
from services import MusicService
from services.ai import GeminiService

logger = logging.getLogger(__name__)


def _secret(value) -> str | None:
    return value.get_secret_value() if value else None


def build_dispatcher(gemini: GeminiService) -> Dispatcher:
    music = MusicService(settings.music_api_url, _secret(settings.music_api_key))

    # Updates from different users run concurrently (a Gemini call takes seconds);
    # updates from one user stay in order, so their FSM steps never race.
    dp = Dispatcher(use_create_task=True, event_isolation=SimpleEventIsolation())
    # Inner middlewares run in registration order, only for updates some handler accepts.
    dp.register_inner_middleware(DbSessionMiddleware(session_factory))
    dp.register_inner_middleware(UserMiddleware())
    dp.register_inner_middleware(ServicesMiddleware(music=music, gemini=gemini))
    dp.include_routers(*get_routers())
    return dp


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
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    gemini = GeminiService.from_settings(settings)
    await init_db()

    bot = Bot(token=settings.bot_token.get_secret_value())
    dp = build_dispatcher(gemini)
    stop_tasks = install_stop_signals(dp)

    logger.info("Bot is starting")
    try:
        await dp.start_polling(bot)
    finally:
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        await bot.close_session()
        await gemini.close()
        await close_db()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())

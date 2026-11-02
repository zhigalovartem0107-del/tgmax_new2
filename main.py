"""Entry point: python main.py

Logs go to the console and to data/bot.log. data/ is the folder the host keeps
between deploys, so when the host's log viewer shows nothing, open that file in
its file manager. The bot is imported only after logging is set up, so even a
configuration error raised on import is written to the file.
"""

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path(__file__).resolve().parent / "data" / "bot.log"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

logger = logging.getLogger("main")


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        )
    except OSError as exc:  # read-only disk: keep console logging
        print(f"Cannot write {LOG_FILE}: {exc}", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)


def run() -> None:
    setup_logging()
    logger.info("Starting, Python %s", sys.version.split()[0])
    try:
        import app  # after logging setup: configuration errors are raised on import

        asyncio.run(app.main())
    except SystemExit as exc:
        if exc.code in (None, 0):
            raise
        logger.critical("Startup failed: %s", exc.code)
        sys.exit(1)
    except Exception:
        logger.critical("Bot crashed", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    run()

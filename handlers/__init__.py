from maxapi import Router

from handlers import fallback, payments, song, start, video
from handlers.start import BOT_COMMANDS

__all__ = ["BOT_COMMANDS", "get_routers"]


def get_routers() -> list[Router]:
    # Order matters: the first router whose filters match handles the update.
    # start goes first so /start, /help and /cancel work in any state; fallback goes last.
    return [start.router, payments.router, song.router, video.router, fallback.router]

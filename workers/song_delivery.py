"""Background job: sends finished songs to their owners, refunds failed or stuck orders.

Every POLL_SECONDS it checks the paid song orders that are not finished yet. The
state lives in the database, so orders in progress survive a bot restart.
"""

import asyncio
import logging
from datetime import timedelta

from maxapi import Bot
from maxapi.enums.upload_type import UploadType
from maxapi.types.input_media import InputMediaBuffer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database import crud
from database.models import Generation, GenerationKind, GenerationStatus, utcnow
from services.music import MusicError, MusicService, MusicStatus

logger = logging.getLogger(__name__)

POLL_SECONDS = 15
SONG_TIMEOUT = timedelta(minutes=20)  # the music server never finished it
SUBMIT_TIMEOUT = timedelta(minutes=5)  # paid, but the bot stopped before the server took it

SONG_READY = "🎶 Твоя песня готова!"
SONG_FAILED = "😔 Не удалось создать песню. Средства вернулись на баланс."


async def deliver_songs(
    bot: Bot, music: MusicService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """One pass over the song orders that are paid but not finished."""
    async with session_factory() as session:
        song_ids = (
            await session.scalars(
                select(Generation.id).where(
                    Generation.kind == GenerationKind.SONG,
                    Generation.status.in_([GenerationStatus.PENDING, GenerationStatus.PROCESSING]),
                )
            )
        ).all()
    for song_id in song_ids:
        try:
            await _check_song(bot, music, session_factory, song_id)
        except Exception:
            logger.exception("Song %s: check failed, will retry", song_id)


async def run_song_delivery(
    bot: Bot,
    music: MusicService,
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = POLL_SECONDS,
) -> None:
    """Runs until cancelled; one failing pass doesn't stop it."""
    while True:
        try:
            await deliver_songs(bot, music, session_factory)
        except Exception:
            logger.exception("Song delivery pass failed")
        await asyncio.sleep(interval)


async def _check_song(
    bot: Bot, music: MusicService, session_factory: async_sessionmaker[AsyncSession], song_id: int
) -> None:
    async with session_factory() as session:
        song = await session.get(Generation, song_id)
        if song is None:  # deleted with its user meanwhile
            return
        age = utcnow() - song.created_at

        if song.status == GenerationStatus.PENDING:
            if age > SUBMIT_TIMEOUT:
                await _refund(bot, session, song, "never reached the music server")
            return

        try:
            result = await music.get_result(song.provider_task_id)
        except MusicError as exc:
            logger.warning("Song %s: music server check failed: %s", song.id, exc)
            result = None

        if result is not None and result.status == MusicStatus.FAILED:
            await _refund(bot, session, song, result.error or "generation failed")
            return
        if result is not None and result.status == MusicStatus.DONE:
            if await _send_song(bot, music, song, result.audio_url):
                song.status = GenerationStatus.DONE
                await session.commit()
                return
        if age > SONG_TIMEOUT:
            await _refund(bot, session, song, "timed out")


async def _send_song(bot: Bot, music: MusicService, song: Generation, audio_url: str) -> bool:
    try:
        audio = await music.download(audio_url)
        await bot.send_message(
            user_id=song.user_id,
            text=SONG_READY,
            attachments=[InputMediaBuffer(audio, filename="song.mp3", type=UploadType.AUDIO)],
        )
    except Exception:
        logger.exception("Song %s: delivery failed, will retry", song.id)
        return False
    return True


async def _refund(bot: Bot, session: AsyncSession, song: Generation, error: str) -> None:
    refunded = await crud.fail_generation(session, song, error)
    await session.commit()  # the refund is stored before the user is told
    if not refunded:  # already refunded (by the handler) or delivered: nothing to tell
        return
    logger.warning("Song %s refunded: %s", song.id, error)
    try:
        await bot.send_message(user_id=song.user_id, text=SONG_FAILED)
    except Exception:
        logger.exception("Song %s: could not tell the user about the refund", song.id)

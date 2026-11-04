"""Background job: sends finished videos to their owners, refunds failed or stuck orders.

Every POLL_SECONDS it checks the paid video orders that are not finished yet. The
state lives in the database, so orders in progress survive a bot restart. A status
check that Agnes answers with 429 or 5xx is simply asked again on the next pass.
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
from services.video import VideoError, VideoService, VideoStatus

logger = logging.getLogger(__name__)

POLL_SECONDS = 20
VIDEO_TIMEOUT = timedelta(minutes=30)  # Agnes never finished it
SUBMIT_TIMEOUT = timedelta(minutes=5)  # paid, but the bot stopped before Agnes took it

VIDEO_READY = "🎬 Твоё видео готово!"
VIDEO_FAILED = "😔 Не удалось создать видео. Средства вернулись на баланс."


async def deliver_videos(
    bot: Bot, video: VideoService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """One pass over the video orders that are paid but not finished."""
    async with session_factory() as session:
        order_ids = (
            await session.scalars(
                select(Generation.id).where(
                    Generation.kind == GenerationKind.VIDEO,
                    Generation.status.in_([GenerationStatus.PENDING, GenerationStatus.PROCESSING]),
                )
            )
        ).all()
    for order_id in order_ids:
        try:
            await _check_video(bot, video, session_factory, order_id)
        except Exception:
            logger.exception("Video %s: check failed, will retry", order_id)


async def run_video_delivery(
    bot: Bot,
    video: VideoService,
    session_factory: async_sessionmaker[AsyncSession],
    interval: float = POLL_SECONDS,
) -> None:
    """Runs until cancelled; one failing pass doesn't stop it."""
    while True:
        try:
            await deliver_videos(bot, video, session_factory)
        except Exception:
            logger.exception("Video delivery pass failed")
        await asyncio.sleep(interval)


async def _check_video(
    bot: Bot, video: VideoService, session_factory: async_sessionmaker[AsyncSession], order_id: int
) -> None:
    async with session_factory() as session:
        order = await session.get(Generation, order_id)
        if order is None:  # deleted with its user meanwhile
            return
        age = utcnow() - order.created_at

        if order.status == GenerationStatus.PENDING:
            if age > SUBMIT_TIMEOUT:
                await _refund(bot, session, order, "never reached Agnes")
            return

        try:
            result = await video.get_result(order.provider_task_id)
        except VideoError as exc:
            logger.warning("Video %s: Agnes check failed: %s", order.id, exc)
            result = None

        if result is not None and result.status == VideoStatus.FAILED:
            await _refund(bot, session, order, result.error or "generation failed")
            return
        if result is not None and result.status == VideoStatus.DONE:
            if await _send_video(bot, video, order, result.video_url):
                order.status = GenerationStatus.DONE
                await session.commit()
                return
        if age > VIDEO_TIMEOUT:
            await _refund(bot, session, order, "timed out")


async def _send_video(bot: Bot, video: VideoService, order: Generation, video_url: str) -> bool:
    try:
        data = await video.download(video_url)
        await bot.send_message(
            user_id=order.user_id,
            text=VIDEO_READY,
            attachments=[InputMediaBuffer(data, filename="video.mp4", type=UploadType.VIDEO)],
        )
    except Exception:
        logger.exception("Video %s: delivery failed, will retry", order.id)
        return False
    return True


async def _refund(bot: Bot, session: AsyncSession, order: Generation, error: str) -> None:
    refunded = await crud.fail_generation(session, order, error)
    await session.commit()  # the refund is stored before the user is told
    if not refunded:  # already refunded (by the handler) or delivered: nothing to tell
        return
    logger.warning("Video %s refunded: %s", order.id, error)
    try:
        await bot.send_message(user_id=order.user_id, text=VIDEO_FAILED)
    except Exception:
        logger.exception("Video %s: could not tell the user about the refund", order.id)

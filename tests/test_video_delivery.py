"""The background job that delivers finished videos and refunds failed or stuck ones."""

import asyncio
from datetime import timedelta

from conftest import USER_ID
from maxapi import Bot
from maxapi.enums.upload_type import UploadType
from maxapi.types.input_media import InputMediaBuffer

import workers.video_delivery as delivery
from database import Generation, GenerationKind, GenerationStatus, crud, session_factory
from database.models import utcnow
from services.video import VideoError, VideoResult, VideoStatus

VIDEO_URL = "https://videos.magichour.ai/id/output.mp4"
MP4 = b"\x00\x00\x00\x18ftypmp42 fake video"


class FakeVideo:
    """Stands in for VideoService; its HTTP side is covered in test_video_service.py."""

    def __init__(self, result=None, error=None, download_error=None):
        self.result, self.error, self.download_error = result, error, download_error
        self.asked = []

    async def get_result(self, task_id):
        self.asked.append(task_id)
        if self.error:
            raise self.error
        return self.result

    async def download(self, video_url):
        assert video_url == VIDEO_URL
        if self.download_error:
            raise self.download_error
        return MP4


async def _paid(session, kind=GenerationKind.VIDEO, status=GenerationStatus.PROCESSING, age=timedelta(0)) -> int:
    """An order of USER_ID, who had 10 and paid 10 for it."""
    await crud.get_or_create_user(session, USER_ID, "Ivan")
    await crud.add_balance(session, USER_ID, 10)
    order = await crud.start_paid_generation(session, USER_ID, kind, "кот на пляже", cost=10, mode="text")
    order.status = status
    order.provider_task_id = "cuid-1" if status == GenerationStatus.PROCESSING else None
    order.created_at = utcnow() - age
    await session.commit()
    return order.id


async def _state(session, order_id):
    session.expunge_all()
    order = await session.get(Generation, order_id)
    return order.status, (await crud.get_user(session, USER_ID)).balance


async def test_finished_video_is_sent_to_its_owner_as_a_video(bot, session):
    order_id = await _paid(session)
    magic_hour = FakeVideo(VideoResult(VideoStatus.DONE, VIDEO_URL))

    await delivery.deliver_videos(bot.bot, magic_hour, session_factory)

    assert magic_hour.asked == ["cuid-1"]
    [message] = bot.sent
    assert message.user_id == USER_ID and message.text == "🎬 Твоё видео готово!"
    [video] = [a for a in message.attachments if isinstance(a, InputMediaBuffer)]
    assert (video.buffer, video.type, video.filename) == (MP4, UploadType.VIDEO, "video.mp4")
    assert await _state(session, order_id) == (GenerationStatus.DONE, 0)


async def test_failed_video_is_refunded_and_owner_told(bot, session):
    order_id = await _paid(session)

    await delivery.deliver_videos(bot.bot, FakeVideo(VideoResult(VideoStatus.FAILED, error="content policy")), session_factory)

    assert await _state(session, order_id) == (GenerationStatus.FAILED, 10)
    [message] = bot.sent
    assert message.user_id == USER_ID
    assert message.text == "😔 Не удалось создать видео. Средства вернулись на баланс."


async def test_video_in_progress_is_left_alone(bot, session):
    order_id = await _paid(session, age=timedelta(minutes=5))

    await delivery.deliver_videos(bot.bot, FakeVideo(VideoResult(VideoStatus.PROCESSING)), session_factory)

    assert await _state(session, order_id) == (GenerationStatus.PROCESSING, 0)
    assert bot.sent == []


async def test_stuck_video_is_refunded(bot, session):
    order_id = await _paid(session, age=delivery.VIDEO_TIMEOUT + timedelta(minutes=1))

    await delivery.deliver_videos(bot.bot, FakeVideo(VideoResult(VideoStatus.PROCESSING)), session_factory)

    assert await _state(session, order_id) == (GenerationStatus.FAILED, 10)


async def test_busy_or_unreachable_api_is_asked_again_later(bot, session):
    """Magic Hour answering 429 or 5xx to a status check is no failure; only the time limit gives up."""
    order_id = await _paid(session, age=timedelta(minutes=5))

    await delivery.deliver_videos(bot.bot, FakeVideo(error=VideoError("Magic Hour HTTP 429: busy")), session_factory)
    assert await _state(session, order_id) == (GenerationStatus.PROCESSING, 0)

    late = FakeVideo(error=VideoError("Magic Hour HTTP 503"))
    async with session_factory() as s:
        order = await s.get(Generation, order_id)
        order.created_at = utcnow() - delivery.VIDEO_TIMEOUT - timedelta(minutes=1)
        await s.commit()
    await delivery.deliver_videos(bot.bot, late, session_factory)
    assert await _state(session, order_id) == (GenerationStatus.FAILED, 10)


async def test_failed_delivery_is_retried_later(bot, session, monkeypatch):
    order_id = await _paid(session, age=timedelta(minutes=5))

    async def send_message(self, *args, **kwargs):
        raise RuntimeError("MAX is down")

    monkeypatch.setattr(Bot, "send_message", send_message)
    await delivery.deliver_videos(bot.bot, FakeVideo(VideoResult(VideoStatus.DONE, VIDEO_URL)), session_factory)
    assert await _state(session, order_id) == (GenerationStatus.PROCESSING, 0)

    await delivery.deliver_videos(
        bot.bot, FakeVideo(VideoResult(VideoStatus.DONE, VIDEO_URL), download_error=VideoError("HTTP 404")), session_factory
    )
    assert await _state(session, order_id) == (GenerationStatus.PROCESSING, 0)


async def test_order_stuck_before_submission_is_refunded(bot, session):
    """Paid, but the bot stopped before Magic Hour took the order."""
    stuck = await _paid(session, status=GenerationStatus.PENDING, age=timedelta(minutes=10))
    submitting = await _paid(session, status=GenerationStatus.PENDING)

    await delivery.deliver_videos(bot.bot, FakeVideo(), session_factory)

    assert (await _state(session, stuck))[0] == GenerationStatus.FAILED
    assert (await _state(session, submitting))[0] == GenerationStatus.PENDING


async def test_songs_are_not_the_video_jobs_business(bot, session):
    song_id = await _paid(session, kind=GenerationKind.SONG)
    magic_hour = FakeVideo(VideoResult(VideoStatus.FAILED, error="nope"))

    await delivery.deliver_videos(bot.bot, magic_hour, session_factory)

    assert magic_hour.asked == [] and await _state(session, song_id) == (GenerationStatus.PROCESSING, 0)


async def test_delivery_loop_survives_a_failing_pass(monkeypatch):
    passes = []

    async def flaky_pass(bot, video, factory):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(delivery, "deliver_videos", flaky_pass)
    loop = asyncio.create_task(delivery.run_video_delivery(None, None, None, interval=0))
    while len(passes) < 3:
        await asyncio.sleep(0)
    loop.cancel()
    assert len(passes) >= 3


def test_polling_interval_is_15_to_30_seconds():
    assert 15 <= delivery.POLL_SECONDS <= 30

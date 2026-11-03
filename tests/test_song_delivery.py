"""The background job that delivers finished songs and refunds failed or stuck ones."""

import asyncio
from datetime import timedelta

from conftest import USER_ID
from maxapi import Bot
from maxapi.enums.upload_type import UploadType
from maxapi.types.input_media import InputMediaBuffer

import workers.song_delivery as delivery
from database import Generation, GenerationKind, GenerationStatus, crud, session_factory
from database.models import utcnow
from services.music import MusicError, MusicResult, MusicStatus

AUDIO_URL = "https://v3.fal.media/files/rabbit/abc123/song.wav"


class FakeMusic:
    """Stands in for MusicService; its HTTP side is covered in test_music.py."""

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def get_result(self, task_id):
        if self.error:
            raise self.error
        return self.result

    async def download(self, audio_url):
        assert audio_url == AUDIO_URL
        return b"RIFF song"


async def _paid_song(session, status=GenerationStatus.PROCESSING, age=timedelta(0)) -> int:
    """A song order for USER_ID who had 10 and paid 10 for it."""
    await crud.get_or_create_user(session, USER_ID, "Ivan")
    await crud.add_balance(session, USER_ID, 10)
    song = await crud.start_paid_generation(
        session, USER_ID, GenerationKind.SONG, "о море", cost=10, style="Рок", lyrics="Текст"
    )
    song.status = status
    song.provider_task_id = "task-1" if status == GenerationStatus.PROCESSING else None
    song.created_at = utcnow() - age
    await session.commit()
    return song.id


async def _state(session, song_id):
    session.expunge_all()
    song = await session.get(Generation, song_id)
    return song.status, (await crud.get_user(session, USER_ID)).balance


async def test_finished_song_is_sent_to_its_owner_as_audio(bot, session):
    song_id = await _paid_song(session)

    await delivery.deliver_songs(bot.bot, FakeMusic(MusicResult(MusicStatus.DONE, AUDIO_URL)), session_factory)

    [message] = bot.sent
    assert message.user_id == USER_ID
    [audio] = [a for a in message.attachments if isinstance(a, InputMediaBuffer)]
    assert (audio.buffer, audio.type, audio.filename) == (b"RIFF song", UploadType.AUDIO, "song.wav")
    assert await _state(session, song_id) == (GenerationStatus.DONE, 0)


async def test_failed_song_is_refunded_and_owner_told(bot, session):
    song_id = await _paid_song(session)

    failed = MusicResult(MusicStatus.FAILED, error="CUDA out of memory")
    await delivery.deliver_songs(bot.bot, FakeMusic(failed), session_factory)

    assert await _state(session, song_id) == (GenerationStatus.FAILED, 10)
    assert [m.user_id for m in bot.sent] == [USER_ID]


async def test_song_in_progress_is_left_alone(bot, session):
    song_id = await _paid_song(session, age=timedelta(minutes=1))

    await delivery.deliver_songs(bot.bot, FakeMusic(MusicResult(MusicStatus.PROCESSING)), session_factory)

    assert await _state(session, song_id) == (GenerationStatus.PROCESSING, 0)
    assert bot.sent == []


async def test_stuck_song_is_refunded(bot, session):
    song_id = await _paid_song(session, age=timedelta(minutes=30))

    await delivery.deliver_songs(bot.bot, FakeMusic(MusicResult(MusicStatus.PROCESSING)), session_factory)

    assert await _state(session, song_id) == (GenerationStatus.FAILED, 10)


async def test_unreachable_music_server_is_retried_later(bot, session):
    song_id = await _paid_song(session, age=timedelta(minutes=1))

    await delivery.deliver_songs(bot.bot, FakeMusic(error=MusicError("unreachable")), session_factory)

    assert await _state(session, song_id) == (GenerationStatus.PROCESSING, 0)


async def test_failed_delivery_is_retried_later(bot, session, monkeypatch):
    song_id = await _paid_song(session, age=timedelta(minutes=1))

    async def send_message(self, *args, **kwargs):
        raise RuntimeError("MAX is down")

    monkeypatch.setattr(Bot, "send_message", send_message)
    await delivery.deliver_songs(bot.bot, FakeMusic(MusicResult(MusicStatus.DONE, AUDIO_URL)), session_factory)

    assert await _state(session, song_id) == (GenerationStatus.PROCESSING, 0)


async def test_order_stuck_before_submission_is_refunded(bot, session):
    """Paid, but the bot stopped before the music server took the order."""
    stuck = await _paid_song(session, status=GenerationStatus.PENDING, age=timedelta(minutes=10))

    await delivery.deliver_songs(bot.bot, FakeMusic(), session_factory)

    assert await _state(session, stuck) == (GenerationStatus.FAILED, 10)


async def test_order_being_submitted_is_left_alone(bot, session):
    submitting = await _paid_song(session, status=GenerationStatus.PENDING)

    await delivery.deliver_songs(bot.bot, FakeMusic(), session_factory)

    assert await _state(session, submitting) == (GenerationStatus.PENDING, 0)


async def test_delivery_loop_survives_a_failing_pass(monkeypatch):
    passes = []

    async def flaky_pass(bot, music, factory):
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(delivery, "deliver_songs", flaky_pass)
    loop = asyncio.create_task(delivery.run_song_delivery(None, None, None, interval=0))
    while len(passes) < 3:
        await asyncio.sleep(0)
    loop.cancel()
    assert len(passes) >= 3


async def test_worker_refund_after_handler_refund_does_not_message_twice(bot, session):
    from database import crud as _crud
    from database.models import GenerationKind
    from workers.song_delivery import SONG_FAILED, _refund

    await _crud.get_or_create_user(session, 4242, "R")
    await _crud.add_balance(session, 4242, 10)
    gen = await _crud.start_paid_generation(session, 4242, GenerationKind.SONG, prompt="p", cost=10)
    await session.commit()
    await _refund(bot.bot, session, gen, "provider error")
    await _refund(bot.bot, session, gen, "timed out")
    assert [s.text for s in bot.sent].count(SONG_FAILED) == 1
    assert (await _crud.get_user(session, 4242)).balance == 10

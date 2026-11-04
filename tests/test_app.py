"""app.main() wiring: the services it builds reach the handlers and the background jobs."""

import asyncio

from maxapi import Dispatcher

import app
from services.video import VideoService


async def test_main_runs_the_video_job_with_the_handlers_agnes_service(monkeypatch):
    started, built, closed = {}, {}, []

    def job(name):
        async def run(bot, service, *args, **kwargs):
            started[name] = service
            await asyncio.Event().wait()  # until main() cancels it

        return run

    real_build = app.build_dispatcher

    def build_dispatcher(*args, **kwargs):
        built.update(kwargs, args=args)
        return real_build(*args, **kwargs)

    async def start_polling(self, bot, *args, **kwargs):
        await asyncio.sleep(0)  # let the jobs start, then "stop"

    async def close(self):
        closed.append(self)

    async def no_commands(bot):
        return None

    monkeypatch.setattr(app, "run_song_delivery", job("songs"))
    monkeypatch.setattr(app, "run_payment_worker", job("payments"))
    monkeypatch.setattr(app, "run_video_delivery", job("videos"))
    monkeypatch.setattr(app, "build_dispatcher", build_dispatcher)
    monkeypatch.setattr(app, "sync_commands", no_commands)
    monkeypatch.setattr(app, "install_stop_signals", lambda dp: set())
    monkeypatch.setattr(Dispatcher, "start_polling", start_polling)
    monkeypatch.setattr(VideoService, "close", close)

    await app.main()

    video = started["videos"]
    assert isinstance(video, VideoService)
    assert built["video"] is video  # the same service, and so the same rate-limit wait, for both
    assert closed == [video]

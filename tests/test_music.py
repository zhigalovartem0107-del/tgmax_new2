"""MusicService against a mocked acemusic.ai API (shapes from ACE-Step's OpenAI-compatible API docs)."""

import asyncio
import base64
import json

import httpx2
import pytest
from pydantic import ValidationError

import services.music as music_module
from config import Settings
from services.music import MusicError, MusicService, MusicStatus, audio_filename

API = "https://api.acemusic.ai"
MODEL = "acemusic/acestep-v15-turbo"
MP3 = b"ID3 fake mp3 bytes"
DATA_URL = "data:audio/mpeg;base64," + base64.b64encode(MP3).decode()
LYRICS = "[Куплет 1]\nКот"


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _sse(*events) -> httpx2.Response:
    body = "".join(f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


STREAM = [  # the documented event sequence
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "\n\n## Metadata\n**Caption:** Рок\n**BPM:** 120"}),
    _chunk({"content": "."}),  # heartbeat, every 2 s while the audio renders
    _chunk({"audio": [{"type": "audio_url", "audio_url": {"url": DATA_URL}}]}),
    _chunk({}, finish_reason="stop"),
    "[DONE]",
]
MODELS = {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


def _api(completion: httpx2.Response, requests: list | None = None):
    """acemusic: GET /v1/models lists the model, POST /v1/chat/completions answers `completion`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path == "/v1/models":
            return httpx2.Response(200, json=MODELS)
        return completion

    return handler


async def _finished(music: MusicService, task_id: str):
    """get_result once the background generation has ended."""
    for _ in range(300):
        result = await music.get_result(task_id)
        if result.status != MusicStatus.PROCESSING:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("generation never finished")


async def test_song_is_generated_and_downloaded():
    requests = []
    music = MusicService("secret", duration=120, http_client=_client(_api(_sse(*STREAM), requests)))

    result = await _finished(music, await music.generate_song(lyrics=LYRICS, style="Рок"))
    audio = await music.download(result.audio_url)
    await music.close()

    assert (result.status, audio) == (MusicStatus.DONE, MP3)
    models, completion = requests
    assert (models.method, str(models.url)) == ("GET", f"{API}/v1/models")
    assert (completion.method, str(completion.url)) == ("POST", f"{API}/v1/chat/completions")
    assert all(r.headers["authorization"] == "Bearer secret" for r in requests)
    body = json.loads(completion.content)
    assert body["model"] == MODEL  # the one /v1/models listed
    assert body["stream"] is True  # heartbeats keep the connection open while the song renders
    assert body["messages"] == [{"role": "user", "content": f"<prompt>Рок</prompt><lyrics>{LYRICS}</lyrics>"}]
    assert body["audio_config"] == {"format": "mp3", "vocal_language": "ru", "duration": 120}


async def test_generate_song_returns_before_the_song_is_ready():
    gate = asyncio.Event()

    async def slow(request: httpx2.Request) -> httpx2.Response:
        await gate.wait()
        return _sse(*STREAM)

    music = MusicService("secret", model=MODEL, http_client=_client(slow))
    task_id = await asyncio.wait_for(music.generate_song(lyrics=LYRICS, style="Рок"), timeout=1)
    assert (await music.get_result(task_id)).status == MusicStatus.PROCESSING

    gate.set()
    assert (await _finished(music, task_id)).status == MusicStatus.DONE
    await music.close()


async def test_configured_model_is_used_without_asking_the_api():
    requests = []
    music = MusicService("secret", model="acemusic/acestep-v1.5-turbo", http_client=_client(_api(_sse(*STREAM), requests)))
    await _finished(music, await music.generate_song(lyrics=LYRICS, style="Рок"))
    await music.close()

    [completion] = requests
    assert json.loads(completion.content)["model"] == "acemusic/acestep-v1.5-turbo"


@pytest.mark.parametrize(
    ("message", "audio_url"),
    [
        (
            {"role": "assistant", "content": "Music generated successfully.",
             "audio": [{"type": "audio_url", "audio_url": {"url": DATA_URL}}]},
            DATA_URL,
        ),
        ({"role": "assistant", "content": "Готово: https://cdn.acemusic.ai/songs/abc.mp3"}, "https://cdn.acemusic.ai/songs/abc.mp3"),
    ],
    ids=["audio-field", "link-in-content"],
)
async def test_plain_json_answers_are_understood(message, audio_url):
    answer = httpx2.Response(
        200,
        json={"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": MODEL,
              "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]},
    )
    music = MusicService("secret", model=MODEL, http_client=_client(_api(answer)))
    result = await _finished(music, await music.generate_song(lyrics=LYRICS, style="Рок"))
    await music.close()

    assert (result.status, result.audio_url) == (MusicStatus.DONE, audio_url)


@pytest.mark.parametrize(
    ("completion", "message"),
    [
        (httpx2.Response(401, json={"detail": "Invalid API key"}), "401"),
        (httpx2.Response(429, json={"detail": "Service busy, queue full"}), "queue full"),
        (_sse(_chunk({"content": "."}), _chunk({}, finish_reason="stop"), "[DONE]"), "no audio"),
    ],
    ids=["unauthorized", "busy", "no-audio"],
)
async def test_failed_generation_is_reported(completion, message):
    music = MusicService("secret", model=MODEL, http_client=_client(_api(completion)))
    result = await _finished(music, await music.generate_song(lyrics=LYRICS, style="Рок"))
    await music.close()

    assert result.status == MusicStatus.FAILED and message in result.error


async def test_unreachable_api_is_reported():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    music = MusicService("secret", model=MODEL, http_client=_client(handler))
    result = await _finished(music, await music.generate_song(lyrics=LYRICS, style="Рок"))
    await music.close()

    assert result.status == MusicStatus.FAILED and "unreachable" in result.error


async def test_hung_generation_gives_up(monkeypatch):
    monkeypatch.setattr(music_module, "GENERATION_TIMEOUT", 0.05)

    async def never(request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()

    music = MusicService("secret", model=MODEL, http_client=_client(never))
    task_id = await asyncio.wait_for(music.generate_song(lyrics=LYRICS, style="Рок"), timeout=1)
    result = await _finished(music, task_id)
    await music.close()

    assert result.status == MusicStatus.FAILED and "took longer" in result.error


async def test_missing_key_fails_at_once():
    music = MusicService(None)
    with pytest.raises(MusicError, match="ACEMUSIC_API_KEY"):
        await music.generate_song(lyrics=LYRICS, style="Рок")
    await music.close()


async def test_job_unknown_after_a_restart_is_failed():
    music = MusicService("secret")
    assert (await music.get_result("job-from-before-the-restart")).status == MusicStatus.FAILED
    await music.close()


async def test_finished_jobs_are_forgotten_after_a_while(monkeypatch):
    music = MusicService("secret", model=MODEL, http_client=_client(_api(_sse(*STREAM))))
    task_id = await music.generate_song(lyrics=LYRICS, style="Рок")
    assert (await _finished(music, task_id)).status == MusicStatus.DONE

    monkeypatch.setattr(music_module, "JOB_TTL", -1)
    assert (await music.get_result(task_id)).status == MusicStatus.FAILED
    await music.close()


async def test_close_stops_running_generations():
    async def never(request: httpx2.Request) -> httpx2.Response:
        await asyncio.Event().wait()

    music = MusicService("secret", model=MODEL, http_client=_client(never))
    await asyncio.wait_for(music.generate_song(lyrics=LYRICS, style="Рок"), timeout=1)
    await asyncio.sleep(0)
    await asyncio.wait_for(music.close(), timeout=1)


async def test_download_of_a_link_sends_no_key_to_other_hosts():
    requests = []
    music = MusicService("secret", http_client=_client(lambda r: requests.append(r) or httpx2.Response(200, content=MP3)))
    assert await music.download("https://cdn.acemusic.ai/songs/abc.mp3") == MP3
    await music.close()
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("url", ["data:audio/mpeg;base64,@@not base64@@", "data:audio/mpeg,plain-text"])
async def test_broken_audio_data_raises_music_error(url):
    music = MusicService("secret")
    with pytest.raises(MusicError):
        await music.download(url)
    await music.close()


async def test_download_rejects_oversized_audio(monkeypatch):
    monkeypatch.setattr(music_module, "MAX_AUDIO_BYTES", 5)
    music = MusicService("secret")
    with pytest.raises(MusicError, match="too large"):
        await music.download(DATA_URL)
    await music.close()


@pytest.mark.parametrize(
    ("url", "filename"),
    [
        (DATA_URL, "song.mp3"),
        ("data:audio/wav;base64,UklGRg==", "song.wav"),
        ("https://cdn.acemusic.ai/songs/abc.wav", "song.wav"),
        ("https://cdn.acemusic.ai/songs/abc.MP3?sig=1", "song.mp3"),
        ("https://cdn.acemusic.ai/songs/abc", "song.mp3"),
    ],
)
def test_audio_filename_follows_the_file_type(url, filename):
    assert audio_filename(url) == filename


@pytest.mark.parametrize("key_variable", ["ACEMUSIC_API_KEY", "MUSIC_API_KEY"])
async def test_settings_come_from_env(monkeypatch, key_variable):
    for name in ("ACEMUSIC_API_KEY", "MUSIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key_variable, "env-key")
    monkeypatch.setenv("MUSIC_MODEL", "acemusic/acestep-v1.5-turbo")
    monkeypatch.setenv("MUSIC_DURATION", "90")
    requests = []

    music = MusicService.from_settings(Settings(_env_file=None), http_client=_client(_api(_sse(*STREAM), requests)))
    await _finished(music, await music.generate_song(lyrics=LYRICS, style="Рок"))
    await music.close()

    [completion] = requests
    body = json.loads(completion.content)
    assert completion.headers["authorization"] == "Bearer env-key"
    assert (body["model"], body["audio_config"]["duration"]) == ("acemusic/acestep-v1.5-turbo", 90)


def test_acemusic_key_wins_over_the_fallback(monkeypatch):
    monkeypatch.setenv("ACEMUSIC_API_KEY", "primary")
    monkeypatch.setenv("MUSIC_API_KEY", "fallback")
    assert Settings(_env_file=None).music_api_key.get_secret_value() == "primary"


@pytest.mark.parametrize("duration", ["0", "600"])
def test_duration_outside_the_allowed_range_is_rejected(monkeypatch, duration):
    monkeypatch.setenv("MUSIC_DURATION", duration)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

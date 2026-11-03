"""MusicService against a mocked fal.ai queue API (shapes from fal's queue and ACE-Step docs)."""

import json

import httpx2
import pytest
from pydantic import ValidationError

import services.music as music_module
from config import Settings
from services.music import MusicError, MusicService, MusicStatus, audio_filename

QUEUE = "https://queue.fal.run"
RESPONSE_URL = f"{QUEUE}/fal-ai/ace-step/requests/req-1"
AUDIO_URL = "https://v3.fal.media/files/rabbit/abc123/song.wav"

SUBMITTED = {
    "request_id": "req-1",
    "response_url": RESPONSE_URL,
    "status_url": f"{RESPONSE_URL}/status",
    "cancel_url": f"{RESPONSE_URL}/cancel",
    "queue_position": 0,
}
RESULT = {"audio": {"url": AUDIO_URL}, "seed": 42, "tags": "Рок", "lyrics": "[Куплет 1]\nКот"}


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_generate_song_queues_lyrics_and_returns_request_url():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=SUBMITTED)

    music = MusicService("secret", model="fal-ai/ace-step", duration=120, http_client=_client(handler))
    assert await music.generate_song(lyrics="[Куплет 1]\nКот", style="Рок") == RESPONSE_URL
    await music.close()

    [request] = requests
    assert (request.method, str(request.url)) == ("POST", f"{QUEUE}/fal-ai/ace-step")
    assert request.headers["authorization"] == "Key secret"
    assert json.loads(request.content) == {"tags": "Рок", "lyrics": "[Куплет 1]\nКот", "duration": 120}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(401, json={"detail": "Invalid API key"}), "401"),
        (httpx2.Response(422, json={"detail": [{"loc": ["body", "tags"], "msg": "field required"}]}), "422"),
        (httpx2.Response(200, json={"request_id": "req-1"}), "response_url"),
        (httpx2.Response(200, json={**SUBMITTED, "response_url": "https://evil.example/r/1"}), "response_url"),
    ],
    ids=["unauthorized", "invalid-input", "no-response-url", "foreign-response-url"],
)
async def test_generate_song_failures_raise_music_error(response, message):
    music = MusicService("secret", http_client=_client(lambda request: response))
    with pytest.raises(MusicError, match=message):
        await music.generate_song(lyrics="x", style="Поп")
    await music.close()


async def test_unreachable_queue_raises_music_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    music = MusicService("secret", http_client=_client(handler))
    with pytest.raises(MusicError, match="unreachable"):
        await music.generate_song(lyrics="x", style="Поп")
    await music.close()


async def test_missing_key_raises_music_error():
    music = MusicService(None)
    with pytest.raises(MusicError, match="MUSIC_API_KEY"):
        await music.generate_song(lyrics="x", style="Поп")
    await music.close()


@pytest.mark.parametrize(
    ("status", "result", "expected_status", "audio_url", "error"),
    [
        (httpx2.Response(202, json={"status": "IN_QUEUE", "queue_position": 3}), None, MusicStatus.PROCESSING, None, None),
        (httpx2.Response(202, json={"status": "IN_PROGRESS"}), None, MusicStatus.PROCESSING, None, None),
        (httpx2.Response(200, json={"status": "COMPLETED"}), httpx2.Response(200, json=RESULT), MusicStatus.DONE, AUDIO_URL, None),
        (
            httpx2.Response(200, json={"status": "COMPLETED", "error": "Runner crashed", "error_type": "runner_error"}),
            None,
            MusicStatus.FAILED,
            None,
            "Runner crashed",
        ),
        (
            httpx2.Response(200, json={"status": "COMPLETED"}),
            httpx2.Response(422, json={"detail": [{"msg": "lyrics too long"}]}),
            MusicStatus.FAILED,
            None,
            "lyrics too long",
        ),
        (httpx2.Response(200, json={"status": "COMPLETED"}), httpx2.Response(404, json={"detail": "Not found"}), MusicStatus.FAILED, None, "404"),
        (httpx2.Response(200, json={"status": "COMPLETED"}), httpx2.Response(200, json={"seed": 1}), MusicStatus.FAILED, None, "no audio"),
    ],
    ids=["queued", "in-progress", "done", "failed-run", "invalid-input", "expired", "no-audio"],
)
async def test_get_result(status, result, expected_status, audio_url, error):
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return status if request.url.path.endswith("/status") else result

    music = MusicService("secret", http_client=_client(handler))
    outcome = await music.get_result(RESPONSE_URL)
    await music.close()

    assert (outcome.status, outcome.audio_url) == (expected_status, audio_url)
    if error:
        assert error in outcome.error
    assert (requests[0].method, str(requests[0].url)) == ("GET", f"{RESPONSE_URL}/status")
    assert all(r.headers["authorization"] == "Key secret" for r in requests)
    if result is not None:
        assert str(requests[1].url) == RESPONSE_URL


async def test_server_error_on_result_is_retried_later():
    """A 5xx when fetching a finished result may be temporary: not a failed song."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/status"):
            return httpx2.Response(200, json={"status": "COMPLETED"})
        return httpx2.Response(500, json={"detail": "Internal Server Error"})

    music = MusicService("secret", http_client=_client(handler))
    with pytest.raises(MusicError, match="500"):
        await music.get_result(RESPONSE_URL)
    await music.close()


async def test_key_is_never_sent_outside_the_queue():
    requests = []
    music = MusicService("secret", http_client=_client(lambda r: requests.append(r) or httpx2.Response(200, json={})))

    with pytest.raises(MusicError):
        await music.get_result("https://evil.example/requests/1")
    await music.download(AUDIO_URL)  # the CDN gets no key either
    await music.close()

    assert [str(r.url) for r in requests] == [AUDIO_URL]
    assert "authorization" not in requests[0].headers


async def test_download_returns_audio_bytes():
    music = MusicService("secret", http_client=_client(lambda r: httpx2.Response(200, content=b"RIFF fake wav")))
    assert await music.download(AUDIO_URL) == b"RIFF fake wav"
    await music.close()


async def test_download_rejects_oversized_audio(monkeypatch):
    monkeypatch.setattr(music_module, "MAX_AUDIO_BYTES", 10)
    music = MusicService("secret", http_client=_client(lambda r: httpx2.Response(200, content=b"x" * 11)))
    with pytest.raises(MusicError, match="too large"):
        await music.download(AUDIO_URL)
    await music.close()


@pytest.mark.parametrize(
    ("url", "filename"),
    [
        (AUDIO_URL, "song.wav"),
        ("https://v3.fal.media/files/a/track.MP3?sig=1", "song.mp3"),
        ("https://v3.fal.media/files/a/no-extension", "song.mp3"),
    ],
)
def test_audio_filename_follows_the_file_type(url, filename):
    assert audio_filename(url) == filename


@pytest.mark.parametrize("key_variable", ["MUSIC_API_KEY", "FAL_KEY"])
async def test_settings_come_from_env(monkeypatch, key_variable):
    for name in ("MUSIC_API_KEY", "FAL_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key_variable, "env-key")
    monkeypatch.setenv("MUSIC_MODEL", "fal-ai/ace-step/prompt-to-audio")
    monkeypatch.setenv("MUSIC_DURATION", "90")
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=SUBMITTED)

    music = MusicService.from_settings(Settings(_env_file=None), http_client=_client(handler))
    await music.generate_song(lyrics="x", style="Поп")
    await music.close()

    [request] = requests
    assert str(request.url) == f"{QUEUE}/fal-ai/ace-step/prompt-to-audio"
    assert request.headers["authorization"] == "Key env-key"
    assert json.loads(request.content)["duration"] == 90


@pytest.mark.parametrize("duration", ["0", "600"])
def test_duration_outside_fal_range_is_rejected(monkeypatch, duration):
    monkeypatch.setenv("MUSIC_DURATION", duration)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

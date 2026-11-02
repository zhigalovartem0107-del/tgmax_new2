"""MusicService against a mocked ACE-Step 1.5 API server (response shapes from its docs/code)."""

import json

import httpx2
import pytest

import services.music as music_module
from services.music import MusicError, MusicService, MusicStatus

BASE = "https://ace.example.com"
AUDIO_PATH = "/v1/audio?path=%2Ftmp%2Fapi_audio%2Fabc.mp3"


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _wrap(data, code=200, error=None) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={"data": data, "code": code, "error": error, "timestamp": 1700000000000, "extra": None},
    )


def _task(status: int, outputs: list) -> dict:
    return {"task_id": "task-1", "status": status, "result": json.dumps(outputs, ensure_ascii=False)}


RUNNING = _task(0, [{"file": "", "status": 0, "progress": 0.4, "stage": "dit", "error": None}])
UNKNOWN = {"task_id": "task-1", "result": "[]", "status": 0}  # what the server says after a restart
DONE = _task(1, [{"file": AUDIO_PATH, "wave": "", "status": 1, "metas": {"duration": 95}}])
DONE_WITHOUT_FILE = _task(1, [{"file": "", "wave": "", "status": 1, "metas": {}}])
FAILED = _task(2, [{"file": "", "status": 2, "stage": "failed", "error": "CUDA out of memory"}])


async def test_generate_song_submits_lyrics_and_returns_task_id():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _wrap({"task_id": "task-1", "status": "queued", "queue_position": 1})

    music = MusicService(BASE, "secret", http_client=_client(handler))
    assert await music.generate_song(lyrics="[Куплет 1]\nКот", style="Рок") == "task-1"
    await music.close()

    [request] = requests
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/release_task")
    assert request.headers["authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert (body["lyrics"], body["prompt"]) == ("[Куплет 1]\nКот", "Рок")
    assert body["vocal_language"] == "ru"
    assert body["batch_size"] == 1  # one song per paid order


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(401, json={"detail": "Invalid API key"}), "401"),
        (httpx2.Response(502, text="Bad Gateway"), "502"),
        (_wrap(None, code=500, error="model not loaded"), "model not loaded"),
        (_wrap({"status": "queued"}), "task_id"),
    ],
    ids=["unauthorized", "bad-gateway", "server-error", "no-task-id"],
)
async def test_generate_song_failures_raise_music_error(response, message):
    music = MusicService(BASE, "secret", http_client=_client(lambda request: response))
    with pytest.raises(MusicError, match=message):
        await music.generate_song(lyrics="x", style="Поп")
    await music.close()


async def test_unreachable_server_raises_music_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    music = MusicService(BASE, None, http_client=_client(handler))
    with pytest.raises(MusicError, match="unreachable"):
        await music.generate_song(lyrics="x", style="Поп")
    await music.close()


async def test_missing_server_url_raises_music_error():
    music = MusicService(None, None)
    with pytest.raises(MusicError, match="MUSIC_API_URL"):
        await music.generate_song(lyrics="x", style="Поп")
    await music.close()


@pytest.mark.parametrize(
    ("item", "status", "audio_url", "error"),
    [
        (RUNNING, MusicStatus.PROCESSING, None, None),
        (UNKNOWN, MusicStatus.PROCESSING, None, None),
        (DONE, MusicStatus.DONE, f"{BASE}{AUDIO_PATH}", None),
        (DONE_WITHOUT_FILE, MusicStatus.FAILED, None, "no audio"),
        (FAILED, MusicStatus.FAILED, None, "CUDA out of memory"),
    ],
    ids=["running", "unknown-task", "done", "done-without-file", "failed"],
)
async def test_get_result(item, status, audio_url, error):
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _wrap([item])

    music = MusicService(BASE, "secret", http_client=_client(handler))
    result = await music.get_result("task-1")
    await music.close()

    assert (result.status, result.audio_url) == (status, audio_url)
    if error:
        assert error in result.error
    [request] = requests
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/query_result")
    assert json.loads(request.content) == {"task_id_list": ["task-1"]}


async def test_download_returns_audio_bytes():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"ID3 fake mp3")

    music = MusicService(BASE, "secret", http_client=_client(handler))
    assert await music.download(f"{BASE}{AUDIO_PATH}") == b"ID3 fake mp3"
    await music.close()
    assert requests[0].headers["authorization"] == "Bearer secret"  # /v1/audio needs the key


async def test_download_does_not_send_key_to_other_hosts():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"ID3")

    music = MusicService(BASE, "secret", http_client=_client(handler))
    await music.download("https://cdn.other.example/song.mp3")
    await music.close()
    assert "authorization" not in requests[0].headers


async def test_download_rejects_oversized_audio(monkeypatch):
    monkeypatch.setattr(music_module, "MAX_AUDIO_BYTES", 10)
    music = MusicService(BASE, None, http_client=_client(lambda r: httpx2.Response(200, content=b"x" * 11)))
    with pytest.raises(MusicError, match="too large"):
        await music.download(f"{BASE}{AUDIO_PATH}")
    await music.close()

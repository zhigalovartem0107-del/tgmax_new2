"""VideoService against a mocked Agnes AI API.

Shapes from the official docs and examples (github.com/AgnesAI-Labs/AgnesAI-Models): POST
/v1/videos creates a task and returns its video_id; GET /agnesapi?video_id=... reports on it.
"""

import json

import httpx2
import pytest

from config import Settings
from database import session_factory
from services.keys import ApiKeys
from services.video import VideoError, VideoRateLimited, VideoResult, VideoService, VideoStatus

CN = "https://apihub.agnes-ai.cn"
COM = "https://apihub.agnes-ai.com"
VIDEO_URL = "https://cdn.agnes-ai.com/videos/video_123.mp4"
CREATED = {"id": "task_123", "video_id": "video_123", "object": "video", "model": "agnes-video-v2.0", "status": "queued"}
MP4 = b"\x00\x00\x00\x18ftypmp42 fake video"


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _service(handler, **kwargs) -> VideoService:
    return VideoService("agnes-key", http_client=_client(handler), **kwargs)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_text_to_video_creates_a_task():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=CREATED)

    video = _service(handler)
    assert await video.generate_video("кот идёт по пляжу на закате") == "video_123"
    await video.close()

    [request] = requests
    assert (request.method, str(request.url)) == ("POST", f"{CN}/v1/videos")
    assert request.headers["authorization"] == "Bearer agnes-key"
    assert json.loads(request.content) == {
        "model": "agnes-video-v2.0",
        "prompt": "кот идёт по пляжу на закате",
        "width": 1152,
        "height": 768,
        "num_frames": 121,  # 5 s at 24 fps
        "frame_rate": 24,
    }


async def test_image_to_video_sends_the_image_link():
    bodies = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=CREATED)

    video = _service(handler)
    await video.generate_video("оживи фото", image_url="https://i.oneme.ru/i?r=abc")
    await video.close()

    assert bodies[0]["image"] == "https://i.oneme.ru/i?r=abc"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"status": "queued"}, VideoResult(VideoStatus.PROCESSING)),
        ({"status": "queued", "error": None}, VideoResult(VideoStatus.PROCESSING)),
        ({"status": "in_progress", "progress": 40}, VideoResult(VideoStatus.PROCESSING)),
        ({"status": "completed", "url": VIDEO_URL}, VideoResult(VideoStatus.DONE, VIDEO_URL)),
        ({"status": "succeeded", "metadata": {"url": VIDEO_URL}}, VideoResult(VideoStatus.DONE, VIDEO_URL)),
        ({"status": "completed", "video_url": VIDEO_URL}, VideoResult(VideoStatus.DONE, VIDEO_URL)),
        ({"status": "completed", "result": {"video_url": VIDEO_URL}}, VideoResult(VideoStatus.DONE, VIDEO_URL)),
    ],
    ids=["queued", "error-null", "in-progress", "url", "metadata-url", "video-url", "result-video-url"],
)
async def test_task_is_polled_by_video_id(answer, expected):
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"id": "task_123", "video_id": "video_123", **answer})

    video = _service(handler)
    assert await video.get_result("video_123") == expected
    await video.close()

    [request] = requests
    assert request.method == "GET"
    assert str(request.url) == f"{CN}/agnesapi?video_id=video_123&model_name=agnes-video-v2.0"
    assert request.headers["authorization"] == "Bearer agnes-key"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"status": "failed", "error": {"message": "content policy violation"}}, "content policy violation"),
        ({"status": "cancelled"}, "cancelled"),
        ({"status": "queued", "error": "prompt rejected"}, "prompt rejected"),
        ({"status": "completed"}, "no video link"),
    ],
    ids=["failed", "cancelled", "error-field", "done-without-link"],
)
async def test_failed_task_says_why(answer, error):
    video = _service(lambda request: httpx2.Response(200, json={"video_id": "video_123", **answer}))
    result = await video.get_result("video_123")
    await video.close()

    assert result.status == VideoStatus.FAILED and error in result.error


async def test_unknown_task_has_failed():
    video = _service(lambda request: httpx2.Response(404, json={"error": {"message": "video not found"}}))
    result = await video.get_result("video_404")
    await video.close()

    assert result.status == VideoStatus.FAILED and "not found" in result.error


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_busy_api_while_polling_is_not_a_failure(status):
    """The delivery job asks again on its next pass; only its own time limit gives up."""
    video = _service(lambda request: httpx2.Response(status, json={"error": {"message": "busy"}}))
    with pytest.raises(VideoError):
        await video.get_result("video_123")
    await video.close()


async def test_legacy_task_id_is_polled_at_its_own_path():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(200, json={"id": "task_9", "status": "queued"})
        assert str(request.url) == f"{CN}/v1/videos/task_9"
        return httpx2.Response(200, json={"id": "task_9", "status": "completed", "url": VIDEO_URL})

    video = _service(handler)
    task_id = await video.generate_video("кот")
    assert task_id == "task_9"
    assert await video.get_result(task_id) == VideoResult(VideoStatus.DONE, VIDEO_URL)
    await video.close()


@pytest.mark.parametrize(("headers", "wait"), [({"Retry-After": "40"}, 40), ({}, 60)], ids=["retry-after", "default"])
async def test_rate_limit_says_when_to_retry_and_isnt_hit_again(headers, wait):
    """Free plan: 1 video a minute. Without Retry-After the wait is that minute."""
    clock = Clock()
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(429, headers=headers, json={"error": {"message": "Too many requests", "code": 429}})

    video = _service(handler, clock=clock)
    with pytest.raises(VideoRateLimited) as caught:
        await video.generate_video("кот")
    assert caught.value.retry_after == pytest.approx(wait)

    clock.now += 10
    with pytest.raises(VideoRateLimited) as again:  # not asked again before the wait is over
        await video.generate_video("пёс")
    assert again.value.retry_after == pytest.approx(wait - 10)
    assert len(requests) == 1
    assert video.wait_before_next() == pytest.approx(wait - 10)
    await video.close()


async def test_rate_limit_lifts_after_the_wait():
    clock = Clock()
    answers = [httpx2.Response(429, headers={"Retry-After": "30"}), httpx2.Response(200, json=CREATED)]
    video = _service(lambda request: answers.pop(0), clock=clock)
    with pytest.raises(VideoRateLimited):
        await video.generate_video("кот")

    clock.now += 31
    assert video.wait_before_next() == 0
    assert await video.generate_video("кот") == "video_123"
    await video.close()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(400, json={"error": {"message": "num_frames must satisfy 8n + 1"}}), "must satisfy"),
        (httpx2.Response(401, json={"error": {"message": "Invalid API key"}}), "401"),
        (httpx2.Response(500, text="<html>oops</html>"), "500"),
        (httpx2.Response(200, json={"error": {"message": "model not available"}}), "model not available"),
        (httpx2.Response(200, json={"status": "queued"}), "no video_id"),
    ],
    ids=["bad-request", "bad-key", "server-error", "error-in-200", "no-id"],
)
async def test_refused_order_is_a_video_error(response, message):
    video = _service(lambda request: response)
    with pytest.raises(VideoError, match=message) as caught:
        await video.generate_video("кот")
    await video.close()
    assert not isinstance(caught.value, VideoRateLimited)


async def test_no_key_is_an_error_without_a_request():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request without a key")

    video = VideoService(None, http_client=_client(handler))
    assert not video.configured
    with pytest.raises(VideoError, match="AGNES_API_KEY"):
        await video.generate_video("кот")
    await video.close()


async def test_blocked_domain_falls_back_to_the_next_one():
    hosts = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        hosts.append(request.url.host)
        if request.url.host == "apihub.agnes-ai.cn":
            raise httpx2.ConnectError("blocked", request=request)
        if request.method == "POST":
            return httpx2.Response(200, json=CREATED)
        return httpx2.Response(200, json={"video_id": "video_123", "status": "queued"})

    video = _service(handler)
    assert await video.generate_video("кот") == "video_123"
    assert (await video.get_result("video_123")).status == VideoStatus.PROCESSING
    await video.close()

    assert hosts == ["apihub.agnes-ai.cn", "apihub.agnes-ai.com", "apihub.agnes-ai.com"]  # the working one stays


async def test_unreachable_api_is_a_video_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    video = _service(handler)
    with pytest.raises(VideoError, match="unreachable"):
        await video.generate_video("кот")
    await video.close()


def test_defaults(monkeypatch):
    for name in ("AGNES_API_KEY", "AGNES_BASE_URL", "AGNES_VIDEO_MODEL"):
        monkeypatch.delenv(name, raising=False)

    video = VideoService.from_settings(Settings(_env_file=None))

    assert video.base_urls == [f"{CN}/v1", f"{COM}/v1"]
    assert video.model == "agnes-video-v2.0"
    assert not video.configured


async def test_settings_and_a_key_saved_in_the_bot(monkeypatch):
    monkeypatch.setenv("AGNES_API_KEY", "env-key")
    monkeypatch.setenv("AGNES_BASE_URL", "https://proxy.example/v1/")
    monkeypatch.setenv("AGNES_VIDEO_MODEL", "agnes-video-2.5-flash")
    cfg = Settings(_env_file=None)
    keys = ApiKeys(session_factory, cfg)
    await keys.load()
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((str(request.url), request.headers["authorization"], json.loads(request.content)["model"]))
        return httpx2.Response(200, json=CREATED)

    video = VideoService.from_settings(cfg, keys=keys, http_client=_client(handler))
    await video.generate_video("кот")
    await keys.set("AGNES_API_KEY", "bot-key")  # saved in /admin: used at once
    await video.generate_video("пёс")
    await video.close()

    assert seen == [
        ("https://proxy.example/v1/videos", "Bearer env-key", "agnes-video-2.5-flash"),
        ("https://proxy.example/v1/videos", "Bearer bot-key", "agnes-video-2.5-flash"),
    ]


async def test_download_returns_the_video_and_sends_the_key_only_to_the_api():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx2.Response(200, content=MP4)

    video = _service(handler)
    assert await video.download(VIDEO_URL) == MP4
    assert await video.download(f"{CN}/files/video_123.mp4") == MP4
    await video.close()

    assert seen == [("cdn.agnes-ai.com", None), ("apihub.agnes-ai.cn", "Bearer agnes-key")]


async def test_failed_or_huge_download_is_a_video_error(monkeypatch):
    import services.video as video_module

    video = _service(lambda request: httpx2.Response(404))
    with pytest.raises(VideoError, match="404"):
        await video.download(VIDEO_URL)
    await video.close()

    monkeypatch.setattr(video_module, "MAX_VIDEO_BYTES", 10)
    video = _service(lambda request: httpx2.Response(200, content=MP4))
    with pytest.raises(VideoError, match="too large"):
        await video.download(VIDEO_URL)
    await video.close()

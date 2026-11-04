"""VideoService against a mocked Magic Hour API.

Shapes from Magic Hour's official Python SDK (github.com/magichourhq/magic-hour-python, 0.91.0),
which is generated from their OpenAPI spec: POST /v1/text-to-video or /v1/image-to-video
starts a video project; GET /v1/video-projects/{id} reports on it; a photo is first put
into their storage through POST /v1/files/upload-urls.
"""

import json

import httpx2
import pytest

from config import Settings
from database import session_factory
from services.keys import ApiKeys
from services.video import VideoError, VideoRateLimited, VideoResult, VideoService, VideoStatus

API = "https://api.magichour.ai"
CREATED = {"id": "cuid-example", "credits_charged": 96, "estimated_frame_cost": 96}
VIDEO_URL = "https://videos.magichour.ai/id/output.mp4"
PHOTO_URL = "https://i.oneme.ru/i?r=photo1"  # how MAX links a photo: no file extension
UPLOAD_URL = "https://videos.magichour.ai/api-assets/id/1234.jpg?X-Amz-Signature=abc"
FILE_PATH = "api-assets/id/1234.jpg"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF fake photo"
PNG = b"\x89PNG\r\n\x1a\n fake photo"
MP4 = b"\x00\x00\x00\x18ftypmp42 fake video"


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _service(handler, **kwargs) -> VideoService:
    return VideoService("mh-key", http_client=_client(handler), **kwargs)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_text_to_video_starts_a_project():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=CREATED)

    video = _service(handler)
    assert await video.generate_video("кот идёт по пляжу на закате") == "cuid-example"
    await video.close()

    [request] = requests
    assert (request.method, str(request.url)) == ("POST", f"{API}/v1/text-to-video")
    assert request.headers["authorization"] == "Bearer mh-key"
    assert json.loads(request.content) == {
        "model": "ltx-2.5",  # the fastest; Magic Hour's default on free accounts
        "end_seconds": 4,  # 4 s × 24 credits at 480p = 96: fits the 100 free daily credits
        "resolution": "480p",
        "aspect_ratio": "9:16",  # a phone screen
        "style": {"prompt": "кот идёт по пляжу на закате"},
    }


async def test_photo_is_uploaded_and_becomes_the_first_frame():
    """Magic Hour takes a link only if its path ends in a file extension; MAX's links don't."""
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if str(request.url) == PHOTO_URL:
            return httpx2.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)
        if request.url.path == "/v1/files/upload-urls":
            return httpx2.Response(
                200, json={"items": [{"upload_url": UPLOAD_URL, "expires_at": "2026-11-05T10:00:00Z", "file_path": FILE_PATH}]}
            )
        if str(request.url) == UPLOAD_URL:
            return httpx2.Response(200)
        return httpx2.Response(200, json=CREATED)

    video = _service(handler)
    assert await video.generate_video("оживи фото", image_url=PHOTO_URL) == "cuid-example"
    await video.close()

    assert [(r.method, str(r.url)) for r in requests] == [
        ("GET", PHOTO_URL),
        ("POST", f"{API}/v1/files/upload-urls"),
        ("PUT", UPLOAD_URL),
        ("POST", f"{API}/v1/image-to-video"),
    ]
    photo, upload_urls, upload, create = requests
    assert json.loads(upload_urls.content) == {"items": [{"type": "image", "extension": "jpg"}]}
    assert upload.content == JPEG
    assert json.loads(create.content) == {
        "model": "ltx-2.5",
        "end_seconds": 4,
        "resolution": "480p",
        "assets": {"image_file_path": FILE_PATH},
        "style": {"prompt": "оживи фото"},
    }
    assert [r.headers.get("authorization") for r in requests] == [None, "Bearer mh-key", None, "Bearer mh-key"]


@pytest.mark.parametrize(
    ("content_type", "content", "extension"),
    [
        ("image/png", PNG, "png"),
        ("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "webp"),
        ("image/heic", b"\x00\x00\x00\x18ftypheic", "heic"),  # an iPhone photo
        ("application/octet-stream", JPEG, "jpg"),
    ],
    ids=["png", "webp", "heic", "jpeg-by-content"],
)
async def test_upload_names_the_photo_type(content_type, content, extension):
    asked = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == PHOTO_URL:
            return httpx2.Response(200, headers={"content-type": content_type}, content=content)
        if request.url.path == "/v1/files/upload-urls":
            asked.append(json.loads(request.content)["items"][0]["extension"])
            return httpx2.Response(200, json={"items": [{"upload_url": UPLOAD_URL, "expires_at": "x", "file_path": FILE_PATH}]})
        return httpx2.Response(200, json=CREATED)

    video = _service(handler)
    await video.generate_video("оживи фото", image_url=PHOTO_URL)
    await video.close()
    assert asked == [extension]


@pytest.mark.parametrize(
    ("upload_urls", "put_status", "message"),
    [({"items": []}, 200, "no upload link"), (None, 403, "photo upload HTTP 403")],
    ids=["no-upload-link", "upload-refused"],
)
async def test_failed_photo_upload_is_an_error_and_orders_nothing(upload_urls, put_status, message):
    asked = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append((request.method, request.url.path))
        if str(request.url) == PHOTO_URL:
            return httpx2.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)
        if request.url.path == "/v1/files/upload-urls":
            item = {"upload_url": UPLOAD_URL, "expires_at": "x", "file_path": FILE_PATH}
            return httpx2.Response(200, json=upload_urls or {"items": [item]})
        if str(request.url) == UPLOAD_URL:
            return httpx2.Response(put_status)
        return httpx2.Response(200, json=CREATED)

    video = _service(handler)
    with pytest.raises(VideoError, match=message):
        await video.generate_video("оживи фото", image_url=PHOTO_URL)
    await video.close()
    assert ("POST", "/v1/image-to-video") not in asked


async def test_photo_that_cant_be_fetched_is_an_error_before_magic_hour():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        return httpx2.Response(404)

    video = _service(handler)
    with pytest.raises(VideoError, match="photo"):
        await video.generate_video("оживи фото", image_url=PHOTO_URL)
    await video.close()
    assert requests == [PHOTO_URL]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"status": "queued"}, VideoResult(VideoStatus.PROCESSING)),
        ({"status": "rendering"}, VideoResult(VideoStatus.PROCESSING)),
        ({"status": "draft"}, VideoResult(VideoStatus.PROCESSING)),
        (
            {"status": "complete", "downloads": [{"url": VIDEO_URL, "expires_at": "2026-11-05T10:00:00Z"}]},
            VideoResult(VideoStatus.DONE, VIDEO_URL),
        ),
        (  # the deprecated single `download`
            {"status": "complete", "downloads": [], "download": {"url": VIDEO_URL, "expires_at": "x"}},
            VideoResult(VideoStatus.DONE, VIDEO_URL),
        ),
    ],
    ids=["queued", "rendering", "draft", "complete", "deprecated-download"],
)
async def test_project_is_polled(answer, expected):
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"id": "cuid-example", "error": None, **answer})

    video = _service(handler)
    assert await video.get_result("cuid-example") == expected
    await video.close()

    [request] = requests
    assert (request.method, str(request.url)) == ("GET", f"{API}/v1/video-projects/cuid-example")
    assert request.headers["authorization"] == "Bearer mh-key"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"status": "error", "error": {"code": "moderation", "message": "The prompt was flagged"}}, "The prompt was flagged"),
        ({"status": "canceled"}, "canceled"),
        ({"status": "complete", "downloads": []}, "no video link"),
    ],
    ids=["error", "canceled", "complete-without-link"],
)
async def test_failed_project_says_why(answer, error):
    video = _service(lambda request: httpx2.Response(200, json={"id": "cuid-example", **answer}))
    result = await video.get_result("cuid-example")
    await video.close()

    assert result.status == VideoStatus.FAILED and error in result.error


async def test_unknown_project_has_failed():
    video = _service(lambda request: httpx2.Response(404, json={"message": "Video project not found"}))
    result = await video.get_result("cuid-404")
    await video.close()

    assert result.status == VideoStatus.FAILED and "not found" in result.error


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_busy_api_while_polling_is_not_a_failure(status):
    """The delivery job asks again on its next pass; only its own time limit gives up."""
    video = _service(lambda request: httpx2.Response(status, json={"message": "busy"}))
    with pytest.raises(VideoError):
        await video.get_result("cuid-example")
    await video.close()


@pytest.mark.parametrize(("headers", "wait"), [({"Retry-After": "40"}, 40), ({}, 60)], ids=["retry-after", "default"])
async def test_rate_limit_says_when_to_retry_and_isnt_hit_again(headers, wait):
    clock = Clock()
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(429, headers=headers, json={"message": "Too many requests"})

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


async def test_rate_limited_photo_upload_is_a_rate_limit_too():
    asked = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked.append(str(request.url))
        if str(request.url) == PHOTO_URL:
            return httpx2.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)
        return httpx2.Response(429, headers={"Retry-After": "20"})

    video = _service(handler)
    with pytest.raises(VideoRateLimited) as caught:
        await video.generate_video("оживи фото", image_url=PHOTO_URL)
    await video.close()
    assert caught.value.retry_after == pytest.approx(20)
    assert asked == [PHOTO_URL, f"{API}/v1/files/upload-urls"]  # stopped at the upload, nothing ordered


async def test_rate_limit_lifts_after_the_wait():
    clock = Clock()
    answers = [httpx2.Response(429, headers={"Retry-After": "30"}), httpx2.Response(200, json=CREATED)]
    video = _service(lambda request: answers.pop(0), clock=clock)
    with pytest.raises(VideoRateLimited):
        await video.generate_video("кот")

    clock.now += 31
    assert video.wait_before_next() == 0
    assert await video.generate_video("кот") == "cuid-example"
    await video.close()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(400, json={"message": "end_seconds is not supported by this model"}), "not supported"),
        (httpx2.Response(401, json={"message": "Unauthorized"}), "401"),
        (httpx2.Response(402, json={"message": "Not enough credits"}), "Not enough credits"),
        (httpx2.Response(500, text="<html>oops</html>"), "500"),
        (httpx2.Response(200, json={"credits_charged": 96}), "no project id"),
    ],
    ids=["bad-request", "bad-key", "no-credits", "server-error", "no-id"],
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
    with pytest.raises(VideoError, match="MAGIC_HOUR_API_KEY"):
        await video.generate_video("кот")
    await video.close()


async def test_unreachable_api_is_a_video_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    video = _service(handler)
    with pytest.raises(VideoError, match="unreachable"):
        await video.generate_video("кот")
    with pytest.raises(VideoError, match="unreachable"):
        await video.get_result("cuid-example")
    await video.close()


def test_defaults(monkeypatch):
    for name in ("MAGIC_HOUR_API_KEY", "MAGIC_HOUR_API_TOKEN", "MAGIC_HOUR_BASE_URL", "MAGIC_HOUR_VIDEO_MODEL", "MAGIC_HOUR_VIDEO_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    video = VideoService.from_settings(Settings(_env_file=None))

    assert (video.base_url, video.model, video.seconds) == (API, "ltx-2.5", 4)
    assert not video.configured


async def test_settings_and_a_key_saved_in_the_bot(monkeypatch):
    monkeypatch.setenv("MAGIC_HOUR_API_KEY", "env-key")
    monkeypatch.setenv("MAGIC_HOUR_BASE_URL", "https://proxy.example/")
    monkeypatch.setenv("MAGIC_HOUR_VIDEO_MODEL", "wan-2.2")
    monkeypatch.setenv("MAGIC_HOUR_VIDEO_SECONDS", "5")
    cfg = Settings(_env_file=None)
    keys = ApiKeys(session_factory, cfg)
    await keys.load()
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers["authorization"], body["model"], body["end_seconds"]))
        return httpx2.Response(200, json=CREATED)

    video = VideoService.from_settings(cfg, keys=keys, http_client=_client(handler))
    await video.generate_video("кот")
    await keys.set("MAGIC_HOUR_API_KEY", "bot-key")  # saved in /admin: used at once
    await video.generate_video("пёс")
    await video.close()

    assert seen == [
        ("https://proxy.example/v1/text-to-video", "Bearer env-key", "wan-2.2", 5),
        ("https://proxy.example/v1/text-to-video", "Bearer bot-key", "wan-2.2", 5),
    ]


def test_the_sdks_token_variable_works_too(monkeypatch):
    monkeypatch.delenv("MAGIC_HOUR_API_KEY", raising=False)
    monkeypatch.setenv("MAGIC_HOUR_API_TOKEN", "sdk-name")
    assert VideoService.from_settings(Settings(_env_file=None)).configured


async def test_download_returns_the_video_and_sends_the_key_only_to_the_api():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx2.Response(200, content=MP4)

    video = _service(handler)
    assert await video.download(VIDEO_URL) == MP4
    assert await video.download(f"{API}/v1/files/video.mp4") == MP4
    await video.close()

    assert seen == [("videos.magichour.ai", None), ("api.magichour.ai", "Bearer mh-key")]


async def test_failed_or_huge_download_is_a_video_error(monkeypatch):
    import services.video as video_module

    video = _service(lambda request: httpx2.Response(403))
    with pytest.raises(VideoError, match="403"):
        await video.download(VIDEO_URL)
    await video.close()

    monkeypatch.setattr(video_module, "MAX_VIDEO_BYTES", 10)
    video = _service(lambda request: httpx2.Response(200, content=MP4))
    with pytest.raises(VideoError, match="too large"):
        await video.download(VIDEO_URL)
    await video.close()

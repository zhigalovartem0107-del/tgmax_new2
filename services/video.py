"""Video generation with Magic Hour (https://magichour.ai): an asynchronous project API.

POST /v1/text-to-video, or /v1/image-to-video, starts a video project and returns its id;
GET /v1/video-projects/{id} reports on it until the video can be downloaded. A photo is
put into Magic Hour's storage first (POST /v1/files/upload-urls, then a PUT of the bytes):
the API takes a link only if its path ends in a file extension, and MAX's photo links don't.

A 429 is raised as VideoRateLimited with the wait (Retry-After, else a minute), and no new
video is ordered before the wait is over.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

import httpx2

from config import Settings
from services.ratelimit import retry_after

API_URL = "https://api.magichour.ai"
DEFAULT_MODEL = "ltx-2.5"  # the fastest; Magic Hour's default on free accounts
DEFAULT_SECONDS = 4  # at 480p a second costs 24 credits: 96 fits the 100 free daily credits
RESOLUTION = "480p"  # the free plan's resolution, and the cheapest
ASPECT_RATIO = "9:16"  # a phone screen; a photo-based video keeps the photo's shape
RATE_LIMIT_WAIT = 60.0  # seconds, when a 429 doesn't say
MAX_PHOTO_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024  # MAX takes videos up to 250 MB

PROCESSING = {"draft", "queued", "rendering"}
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "image/gif": "gif"}


class VideoError(Exception):
    """The video API failed or answered something unusable."""


class VideoRateLimited(VideoError):
    """The API took no new video: too many requests. retry_after: seconds to wait."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Magic Hour rate limit, retry after {retry_after:.0f} s")


class VideoStatus(StrEnum):
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class VideoResult:
    status: VideoStatus
    video_url: str | None = None
    error: str | None = None


class VideoService:
    def __init__(
        self,
        api_key: str | Callable[[], str | None] | None,  # a key, or where to read the current one
        model: str = DEFAULT_MODEL,
        seconds: int = DEFAULT_SECONDS,
        base_url: str = API_URL,
        http_client: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self.seconds = seconds
        self.base_url = base_url.rstrip("/")
        self._key = api_key if callable(api_key) else (lambda: api_key)
        self._http = http_client or httpx2.AsyncClient(timeout=60.0)
        self._clock = clock
        self._blocked_until = 0.0  # no new order before this, after a 429

    @classmethod
    def from_settings(cls, cfg: Settings, keys=None, **kwargs) -> "VideoService":
        """MAGIC_HOUR_API_KEY, read on every request from `keys` (services.keys.ApiKeys) when
        given, so a key saved in the bot is used at once; MAGIC_HOUR_VIDEO_MODEL,
        MAGIC_HOUR_VIDEO_SECONDS and MAGIC_HOUR_BASE_URL."""
        if keys is not None:
            api_key = keys.getter("MAGIC_HOUR_API_KEY")
        else:
            api_key = cfg.magic_hour_api_key.get_secret_value() if cfg.magic_hour_api_key else None
        return cls(
            api_key,
            model=cfg.magic_hour_video_model or DEFAULT_MODEL,
            seconds=cfg.magic_hour_video_seconds or DEFAULT_SECONDS,
            base_url=cfg.magic_hour_base_url or API_URL,
            **kwargs,
        )

    @property
    def configured(self) -> bool:
        return bool(self._key())

    def wait_before_next(self) -> float:
        """Seconds until a new video may be ordered (0: now) after the API said "too many requests"."""
        return max(0.0, self._blocked_until - self._clock())

    async def generate_video(self, prompt: str, image_url: str | None = None) -> str:
        """Start a video and return its project id for get_result(). image_url: the first frame."""
        if not self.configured:
            raise VideoError("MAGIC_HOUR_API_KEY is not set")
        if wait := self.wait_before_next():
            raise VideoRateLimited(wait)

        body: dict = {"model": self.model, "end_seconds": self.seconds, "resolution": RESOLUTION}
        if image_url:
            body["assets"] = {"image_file_path": await self._upload_photo(image_url)}
            path = "/v1/image-to-video"
        else:
            body["aspect_ratio"] = ASPECT_RATIO
            path = "/v1/text-to-video"
        body["style"] = {"prompt": prompt}

        answer = await self._call("POST", path, json=body)
        project_id = answer.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise VideoError(f"Magic Hour answered with no project id: {str(answer)[:200]}")
        return project_id

    async def get_result(self, task_id: str) -> VideoResult:
        """The project's state. Raises VideoError when the API can't tell now (busy, 5xx): ask again later."""
        response = await self._request("GET", f"/v1/video-projects/{task_id}")
        body = _json(response)
        if response.status_code == 404:
            return VideoResult(VideoStatus.FAILED, error=f"video project not found: {_detail(response, body)}")
        if response.status_code != 200:
            raise VideoError(f"Magic Hour HTTP {response.status_code}: {_detail(response, body)}")

        status = str(body.get("status") or "").lower()
        if status in PROCESSING:
            return VideoResult(VideoStatus.PROCESSING)
        if status == "complete":
            url = _video_url(body)
            if url is None:
                return VideoResult(VideoStatus.FAILED, error="complete, but no video link")
            return VideoResult(VideoStatus.DONE, video_url=url)
        return VideoResult(VideoStatus.FAILED, error=_error_text(body.get("error")) or status or "no status")

    async def download(self, video_url: str) -> bytes:
        headers = self._auth() if self._is_api(video_url) else {}  # the key goes only to the API
        video, _content_type = await self._fetch(video_url, headers, MAX_VIDEO_BYTES, "video")
        return video

    async def close(self) -> None:
        await self._http.aclose()

    async def _upload_photo(self, photo_url: str) -> str:
        """Put the photo into Magic Hour's storage; returns its file_path for `assets`."""
        photo, content_type = await self._fetch(photo_url, {}, MAX_PHOTO_BYTES, "photo")
        extension = PHOTO_TYPES.get(content_type) or _photo_extension(photo)
        answer = await self._call("POST", "/v1/files/upload-urls", json={"items": [{"type": "image", "extension": extension}]})
        items = answer.get("items") or []
        item = items[0] if items and isinstance(items[0], Mapping) else {}
        if not item.get("upload_url") or not item.get("file_path"):
            raise VideoError(f"Magic Hour gave no upload link: {str(answer)[:200]}")
        try:
            upload = await self._http.put(item["upload_url"], content=photo)  # a signed link: no key
        except httpx2.HTTPError as exc:
            raise VideoError(f"photo upload failed: {exc}") from exc
        if upload.status_code not in (200, 201, 204):
            raise VideoError(f"photo upload HTTP {upload.status_code}")
        return item["file_path"]

    async def _call(self, method: str, path: str, **kwargs) -> dict:
        """An order-side request: a 429 blocks new orders for the wait it names."""
        response = await self._request(method, path, **kwargs)
        body = _json(response)
        if response.status_code == 429:
            wait = retry_after(response, body.get("error")) or RATE_LIMIT_WAIT
            self._blocked_until = self._clock() + wait
            raise VideoRateLimited(wait)
        if response.status_code not in (200, 201, 202):
            raise VideoError(f"Magic Hour HTTP {response.status_code}: {_detail(response, body)}")
        return body

    async def _request(self, method: str, path: str, **kwargs) -> httpx2.Response:
        try:
            return await self._http.request(method, self.base_url + path, headers=self._auth(), **kwargs)
        except httpx2.HTTPError as exc:
            raise VideoError(f"Magic Hour unreachable: {exc}") from exc

    async def _fetch(self, url: str, headers: dict, limit: int, what: str) -> tuple[bytes, str]:
        """The file at `url`, at most `limit` bytes, and its MIME type."""
        try:
            async with self._http.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    raise VideoError(f"{what} download HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                return await _read(response, limit, what), content_type
        except httpx2.HTTPError as exc:
            raise VideoError(f"{what} download failed: {exc}") from exc

    def _auth(self) -> dict[str, str]:
        key = self._key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _is_api(self, url: str) -> bool:
        return urlparse(url).hostname == urlparse(self.base_url).hostname


async def _read(response: httpx2.Response, limit: int, what: str) -> bytes:
    chunks, size = [], 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise VideoError(f"{what} is too large: over {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _photo_extension(photo: bytes) -> str:
    """The photo's type from its first bytes, when the link didn't say."""
    if photo.startswith(b"\x89PNG"):
        return "png"
    if photo[:4] == b"RIFF" and photo[8:12] == b"WEBP":
        return "webp"
    if photo.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return "jpg"  # what MAX serves photos as


def _json(response: httpx2.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_text(value: object) -> str | None:
    if isinstance(value, Mapping):
        return str(value.get("message") or value.get("code") or value)[:300] if value else None
    return str(value)[:300] if value else None


def _detail(response: httpx2.Response, body: dict) -> str:
    return _error_text(body.get("error")) or str(body.get("message") or response.text[:200])


def _video_url(body: dict) -> str | None:
    """The first download link; `download` is the deprecated single one."""
    downloads = body.get("downloads") if isinstance(body.get("downloads"), list) else []
    download = body.get("download") if isinstance(body.get("download"), Mapping) else None
    candidates = [*(d.get("url") for d in downloads if isinstance(d, Mapping)), download and download.get("url")]
    return next((c for c in candidates if isinstance(c, str) and c.startswith(("https://", "http://"))), None)

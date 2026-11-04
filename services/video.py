"""Video generation with Agnes AI (agnes-video-v2.0), an asynchronous task API.

POST {base}/videos starts a task and returns its video_id; GET {root}/agnesapi?video_id=...
reports on it until the video is ready, as a link. The free plan takes 1 video request a
minute: a 429 is raised as VideoRateLimited with the wait (Retry-After, else that minute),
and no new order is sent before the wait is over.

apihub.agnes-ai.cn is the current API domain. apihub.agnes-ai.com, the international one,
serves the same API with the same keys and is tried when .cn can't be reached.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

import httpx2

from config import Settings
from services.ratelimit import retry_after

API_URLS = ("https://apihub.agnes-ai.cn/v1", "https://apihub.agnes-ai.com/v1")  # tried in order
DEFAULT_MODEL = "agnes-video-v2.0"
WIDTH, HEIGHT = 1152, 768  # the documented default size, 3:2
NUM_FRAMES = 121  # 8n + 1 frames, at most 441; 121 at 24 fps is about 5 s
FRAME_RATE = 24
RATE_LIMIT_WAIT = 60.0  # seconds, when a 429 doesn't say: the free plan takes 1 video a minute
MAX_VIDEO_BYTES = 200 * 1024 * 1024  # MAX takes videos up to 250 MB

DONE = {"completed", "succeeded", "success", "done"}
FAILED = {"failed", "error", "cancelled", "canceled"}


class VideoError(Exception):
    """The video API failed or answered something unusable."""


class VideoRateLimited(VideoError):
    """The API took no new video: too many requests. retry_after: seconds to wait."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Agnes rate limit, retry after {retry_after:.0f} s")


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
        base_urls: Sequence[str] = API_URLS,
        http_client: httpx2.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self.base_urls = [url.rstrip("/") for url in base_urls]
        self._key = api_key if callable(api_key) else (lambda: api_key)
        self._http = http_client or httpx2.AsyncClient(timeout=60.0)
        self._clock = clock
        self._current = 0  # the base URL that answered last
        self._blocked_until = 0.0  # no new order before this, after a 429

    @classmethod
    def from_settings(cls, cfg: Settings, keys=None, **kwargs) -> "VideoService":
        """AGNES_API_KEY, read on every request from `keys` (services.keys.ApiKeys) when given,
        so a key saved in the bot is used at once; AGNES_BASE_URL (comma-separated, tried in
        order) and AGNES_VIDEO_MODEL."""
        if keys is not None:
            api_key = keys.getter("AGNES_API_KEY")
        else:
            api_key = cfg.agnes_api_key.get_secret_value() if cfg.agnes_api_key else None
        urls = [url.strip() for url in (cfg.agnes_base_url or "").split(",") if url.strip()]
        return cls(api_key, model=cfg.agnes_video_model or DEFAULT_MODEL, base_urls=urls or API_URLS, **kwargs)

    @property
    def configured(self) -> bool:
        return bool(self._key())

    def wait_before_next(self) -> float:
        """Seconds until a new video may be ordered (0: now) after the API said "too many requests"."""
        return max(0.0, self._blocked_until - self._clock())

    async def generate_video(self, prompt: str, image_url: str | None = None) -> str:
        """Start a video and return its id for get_result(). image_url: a public link to the first frame."""
        if not self.configured:
            raise VideoError("AGNES_API_KEY is not set")
        if wait := self.wait_before_next():
            raise VideoRateLimited(wait)

        payload = {
            "model": self.model,
            "prompt": prompt,
            "width": WIDTH,
            "height": HEIGHT,
            "num_frames": NUM_FRAMES,
            "frame_rate": FRAME_RATE,
        }
        if image_url:
            payload["image"] = image_url
        response = await self._request("POST", "/videos", json=payload)
        body = _json(response)
        if response.status_code == 429:
            wait = retry_after(response, body.get("error")) or RATE_LIMIT_WAIT
            self._blocked_until = self._clock() + wait
            raise VideoRateLimited(wait)
        if response.status_code not in (200, 201, 202) or body.get("error"):
            raise VideoError(f"Agnes HTTP {response.status_code}: {_detail(response, body)}")

        video_id = body.get("video_id") or body.get("task_id") or body.get("id")
        if not isinstance(video_id, str) or not video_id:
            raise VideoError(f"Agnes answered with no video_id: {str(body)[:200]}")
        return video_id

    async def get_result(self, task_id: str) -> VideoResult:
        """The task's state. Raises VideoError when the API can't tell now (busy, 5xx): ask again later."""
        if task_id.startswith("task_"):  # a legacy task id has its own path
            response = await self._request("GET", f"/videos/{task_id}")
        else:
            params = {"video_id": task_id, "model_name": self.model}
            response = await self._request("GET", "/agnesapi", root=True, params=params)
        body = _json(response)
        if response.status_code == 404:
            return VideoResult(VideoStatus.FAILED, error=f"video task not found: {_detail(response, body)}")
        if response.status_code != 200:
            raise VideoError(f"Agnes HTTP {response.status_code}: {_detail(response, body)}")

        status = str(body.get("status") or "").lower()
        error = _error_text(body.get("error"))
        if status in FAILED or error:
            return VideoResult(VideoStatus.FAILED, error=error or status)
        if status in DONE:
            url = _video_url(body)
            if url is None:
                return VideoResult(VideoStatus.FAILED, error="finished with no video link")
            return VideoResult(VideoStatus.DONE, video_url=url)
        return VideoResult(VideoStatus.PROCESSING)

    async def download(self, video_url: str) -> bytes:
        headers = self._auth() if self._is_api(video_url) else {}  # the key goes only to the API
        chunks, size = [], 0
        try:
            async with self._http.stream("GET", video_url, headers=headers) as response:
                if response.status_code != 200:
                    raise VideoError(f"video download HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_VIDEO_BYTES:
                        raise VideoError(f"video is too large: over {MAX_VIDEO_BYTES} bytes")
                    chunks.append(chunk)
        except httpx2.HTTPError as exc:
            raise VideoError(f"video download failed: {exc}") from exc
        return b"".join(chunks)

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, root: bool = False, **kwargs) -> httpx2.Response:
        """Ask the first base URL that can be reached, starting with the one that answered last."""
        failures = []
        for index in [*range(self._current, len(self.base_urls)), *range(self._current)]:
            base = self.base_urls[index]
            url = (_root(base) if root else base) + path
            try:
                response = await self._http.request(method, url, headers=self._auth(), **kwargs)
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:  # blocked or down: nothing was sent
                failures.append(f"{urlparse(base).hostname}: {exc}")
                continue
            except httpx2.HTTPError as exc:
                raise VideoError(f"Agnes unreachable: {exc}") from exc
            self._current = index
            return response
        raise VideoError(f"Agnes unreachable: {'; '.join(failures)}")

    def _auth(self) -> dict[str, str]:
        key = self._key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _is_api(self, url: str) -> bool:
        return urlparse(url).hostname in {urlparse(base).hostname for base in self.base_urls}


def _root(base: str) -> str:
    """https://host/v1 -> https://host: the results endpoint is outside /v1."""
    return base[: -len("/v1")] if base.endswith("/v1") else base


def _json(response: httpx2.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_text(value: object) -> str | None:
    if isinstance(value, Mapping):
        return str(value.get("message") or value)[:300] if value else None
    return str(value)[:300] if value else None


def _detail(response: httpx2.Response, body: dict) -> str:
    return _error_text(body.get("error")) or str(body.get("message") or response.text[:200])


def _video_url(body: dict) -> str | None:
    """The video link: Agnes puts it in one of several fields."""
    metadata = body.get("metadata") if isinstance(body.get("metadata"), Mapping) else {}
    result = body.get("result") if isinstance(body.get("result"), Mapping) else {}
    candidates = [
        metadata.get("url"),
        body.get("video_url"),
        body.get("url"),
        body.get("output_url"),
        result.get("video_url"),
        body.get("remixed_from_video_id"),  # where the public playground reads it from
    ]
    return next((c for c in candidates if isinstance(c, str) and c.startswith(("https://", "http://"))), None)

"""Song generation with ACE-Step on fal.ai, through fal's queue API.

1. POST https://queue.fal.run/{model} queues the job; its `response_url` is our task id.
2. GET {response_url}/status says IN_QUEUE / IN_PROGRESS (HTTP 202) or COMPLETED (200).
   A failed run is COMPLETED too, with `error` set.
3. GET {response_url} returns the output: {"audio": {"url": ...}, ...} or {"detail": ...}.
4. The audio file is downloaded from fal's CDN, which needs no key.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx2

from config import Settings

QUEUE_URL = "https://queue.fal.run"
DEFAULT_MODEL = "fal-ai/ace-step"
DEFAULT_DURATION = 180  # seconds of audio; fal bills per second
MAX_AUDIO_BYTES = 100 * 1024 * 1024  # a 4-minute WAV is about 40 MB

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}


class MusicError(Exception):
    """The music API failed or answered something unusable."""


class MusicStatus(StrEnum):
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class MusicResult:
    status: MusicStatus
    audio_url: str | None = None
    error: str | None = None


def audio_filename(audio_url: str) -> str:
    """'song' plus the file's own extension, so a WAV isn't sent as song.mp3."""
    extension = PurePosixPath(urlparse(audio_url).path).suffix.lower()
    return f"song{extension if extension in AUDIO_EXTENSIONS else '.mp3'}"


class MusicService:
    def __init__(
        self,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        duration: int = DEFAULT_DURATION,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.duration = duration
        self._auth = {"Authorization": f"Key {api_key}"} if api_key else {}
        self._http = http_client or httpx2.AsyncClient(timeout=60.0)

    @classmethod
    def from_settings(cls, cfg: Settings, **kwargs) -> "MusicService":
        """Build from MUSIC_API_KEY (or FAL_KEY), MUSIC_MODEL and MUSIC_DURATION."""
        return cls(
            cfg.music_api_key.get_secret_value() if cfg.music_api_key else None,
            model=cfg.music_model or DEFAULT_MODEL,
            duration=cfg.music_duration or DEFAULT_DURATION,
            **kwargs,
        )

    async def generate_song(self, lyrics: str, style: str) -> str:
        """Queue a generation job and return its id (fal's response_url)."""
        if not self._auth:
            raise MusicError("MUSIC_API_KEY is not set")
        response = await self._request(
            "POST",
            f"{QUEUE_URL}/{self.model}",
            json={"tags": style, "lyrics": lyrics, "duration": self.duration},
        )
        if not response.is_success:
            raise MusicError(f"fal HTTP {response.status_code}: {_detail(response)}")
        response_url = _json(response).get("response_url")
        if not isinstance(response_url, str) or not _is_queue_url(response_url):
            raise MusicError(f"no usable response_url in fal's answer: {response.text[:200]}")
        return response_url

    async def get_result(self, task_id: str) -> MusicResult:
        if not _is_queue_url(task_id):  # never send the key anywhere else
            raise MusicError(f"not a fal queue request: {task_id!r}")

        status_response = await self._request("GET", f"{task_id}/status")
        if status_response.status_code not in (200, 202):
            raise MusicError(f"fal status HTTP {status_response.status_code}: {_detail(status_response)}")
        status = _json(status_response)
        if status.get("status") in ("IN_QUEUE", "IN_PROGRESS"):
            return MusicResult(MusicStatus.PROCESSING)
        if status.get("status") != "COMPLETED":
            raise MusicError(f"unexpected fal status: {status_response.text[:200]}")
        if status.get("error"):
            return MusicResult(MusicStatus.FAILED, error=f"{status.get('error_type')}: {status['error']}")

        result = await self._request("GET", task_id)
        if result.status_code >= 500:  # may be temporary: let the worker retry
            raise MusicError(f"fal result HTTP {result.status_code}: {_detail(result)}")
        if result.status_code != 200:
            return MusicResult(MusicStatus.FAILED, error=f"HTTP {result.status_code}: {_detail(result)}")
        audio = _json(result).get("audio")
        audio_url = audio.get("url") if isinstance(audio, dict) else None
        if not audio_url:
            return MusicResult(MusicStatus.FAILED, error="no audio in the result")
        return MusicResult(MusicStatus.DONE, audio_url=audio_url)

    async def download(self, audio_url: str) -> bytes:
        response = await self._request("GET", audio_url)
        if response.status_code != 200:
            raise MusicError(f"audio download HTTP {response.status_code}")
        if len(response.content) > MAX_AUDIO_BYTES:
            raise MusicError(f"audio is too large: {len(response.content)} bytes")
        return response.content

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx2.Response:
        headers = self._auth if _is_queue_url(url) else {}
        try:
            return await self._http.request(method, url, headers=headers, **kwargs)
        except httpx2.HTTPError as exc:
            raise MusicError(f"music API unreachable: {exc}") from exc


def _is_queue_url(url: str) -> bool:
    return url.startswith(QUEUE_URL + "/")


def _json(response: httpx2.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise MusicError(f"fal answered with non-JSON: {response.text[:200]}") from exc
    return body if isinstance(body, dict) else {}


def _detail(response: httpx2.Response) -> str:
    """fal's error text: {"detail": "..."} or a list of validation errors."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return response.text[:200]
    if isinstance(detail, list):
        return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail or response.text[:200])

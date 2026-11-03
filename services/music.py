"""Song generation with ACE-Step 1.5 on acemusic.ai (OpenAI-compatible chat API).

The API is synchronous: one POST /v1/chat/completions returns the finished song,
base64-encoded in choices[0].message.audio, after minutes of generation. The bot's
flow is submit -> poll -> download, so generate_song() runs that request in the
background and returns a job id at once; get_result() reports on the job and
download() decodes the audio. The request is streamed: during generation the
server sends a heartbeat every 2 seconds, so proxies don't cut a silent connection
(the hosted API is known to answer 504 to long non-streamed requests).

Jobs live in this process: after a restart get_result() reports them as failed,
and the delivery job refunds them.
"""

import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx2

from config import Settings

API_URL = "https://api.acemusic.ai"
DEFAULT_DURATION = 180  # seconds of audio
GENERATION_TIMEOUT = 15 * 60  # seconds; the delivery job refunds after 20 minutes anyway
JOB_TTL = 60 * 60  # how long a finished job is kept for the delivery job to collect
MAX_AUDIO_BYTES = 100 * 1024 * 1024

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}
_LINK = re.compile(r"https?://[^\s)\"'<>]+")


class MusicError(Exception):
    """The music API failed or answered something unusable."""


class MusicStatus(StrEnum):
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class MusicResult:
    status: MusicStatus
    audio_url: str | None = None  # a data: URL with the audio itself, or a link to it
    error: str | None = None


def audio_filename(audio_url: str) -> str:
    """'song' plus the audio's own extension, so a WAV isn't sent as song.mp3."""
    if audio_url.startswith("data:"):
        mime = audio_url[5:].split(",", 1)[0].split(";", 1)[0].lower()
        return f"song{MIME_EXTENSIONS.get(mime, '.mp3')}"
    extension = PurePosixPath(urlparse(audio_url).path).suffix.lower()
    return f"song{extension if extension in AUDIO_EXTENSIONS else '.mp3'}"


@dataclass
class _Job:
    task: asyncio.Task
    finished_at: float | None = None


class MusicService:
    def __init__(
        self,
        api_key: str | Callable[[], str | None] | None,  # a key, or where to read the current one
        model: str | None = None,  # None: the first model GET /v1/models lists
        duration: int = DEFAULT_DURATION,
        http_client: httpx2.AsyncClient | None = None,
        base_url: str = API_URL,
    ) -> None:
        self.model = model
        self.duration = duration
        self.base_url = base_url.rstrip("/")
        self._key = api_key if callable(api_key) else (lambda: api_key)
        # 60 s between bytes is plenty: the stream carries a heartbeat every 2 s.
        self._http = http_client or httpx2.AsyncClient(timeout=60.0)
        self._jobs: dict[str, _Job] = {}

    @classmethod
    def from_settings(cls, cfg: Settings, keys=None, **kwargs) -> "MusicService":
        """Build from MUSIC_MODEL and MUSIC_DURATION; the key is ACEMUSIC_API_KEY (or MUSIC_API_KEY),
        read on every request from `keys` (services.keys.ApiKeys) when given, so a key saved
        in the bot is used at once."""
        return cls(
            keys.getter("ACEMUSIC_API_KEY") if keys is not None
            else (cfg.music_api_key.get_secret_value() if cfg.music_api_key else None),
            model=cfg.music_model,
            duration=cfg.music_duration or DEFAULT_DURATION,
            **kwargs,
        )

    async def generate_song(self, lyrics: str, style: str) -> str:
        """Start generating in the background and return the job id right away."""
        if not self._auth():
            raise MusicError("ACEMUSIC_API_KEY is not set")
        self._forget_old_jobs()
        task_id = uuid.uuid4().hex
        job = _Job(asyncio.create_task(self._generate(lyrics, style)))
        job.task.add_done_callback(lambda _task: setattr(job, "finished_at", time.monotonic()))
        self._jobs[task_id] = job
        return task_id

    async def get_result(self, task_id: str) -> MusicResult:
        self._forget_old_jobs()
        job = self._jobs.get(task_id)
        if job is None:
            return MusicResult(MusicStatus.FAILED, error="unknown job: the bot restarted during generation?")
        if not job.task.done():
            return MusicResult(MusicStatus.PROCESSING)
        if job.task.cancelled():
            return MusicResult(MusicStatus.FAILED, error="generation was cancelled")
        if job.task.exception() is not None:
            return MusicResult(MusicStatus.FAILED, error=str(job.task.exception()))
        return MusicResult(MusicStatus.DONE, audio_url=job.task.result())

    async def download(self, audio_url: str) -> bytes:
        if audio_url.startswith("data:"):
            audio = _decode_data_url(audio_url)
        else:
            response = await self._request("GET", audio_url)
            if response.status_code != 200:
                raise MusicError(f"audio download HTTP {response.status_code}")
            audio = response.content
        if len(audio) > MAX_AUDIO_BYTES:
            raise MusicError(f"audio is too large: {len(audio)} bytes")
        return audio

    async def close(self) -> None:
        for job in self._jobs.values():
            job.task.cancel()
        await asyncio.gather(*(job.task for job in self._jobs.values()), return_exceptions=True)
        await self._http.aclose()

    async def _generate(self, lyrics: str, style: str) -> str:
        """One generation; returns the audio as a data: URL (or a link to it)."""
        try:
            async with asyncio.timeout(GENERATION_TIMEOUT):
                payload = {
                    "model": self.model or await self._discover_model(),
                    "messages": [{"role": "user", "content": f"<prompt>{style}</prompt><lyrics>{lyrics}</lyrics>"}],
                    "stream": True,
                    "audio_config": {"format": "mp3", "vocal_language": "ru", "duration": self.duration},
                }
                async with self._http.stream(
                    "POST", f"{self.base_url}/v1/chat/completions", json=payload, headers=self._auth()
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise MusicError(f"acemusic HTTP {response.status_code}: {_detail(response)}")
                    if response.headers.get("content-type", "").startswith("text/event-stream"):
                        audio_url, text = await _read_stream(response)
                    else:
                        await response.aread()
                        audio_url, text = _read_message(_json(response))
        except TimeoutError:
            raise MusicError(f"generation took longer than {GENERATION_TIMEOUT} s") from None
        except httpx2.HTTPError as exc:
            raise MusicError(f"acemusic unreachable: {exc}") from exc

        audio_url = audio_url or _audio_link(text)
        if not audio_url:
            raise MusicError("no audio in acemusic's answer")
        return audio_url

    async def _discover_model(self) -> str:
        response = await self._request("GET", f"{self.base_url}/v1/models")
        if response.status_code != 200:
            raise MusicError(f"acemusic /v1/models HTTP {response.status_code}: {_detail(response)}")
        models = _json(response).get("data") or []
        model = next((m["id"] for m in models if isinstance(m, dict) and m.get("id")), None)
        if model is None:
            raise MusicError("acemusic lists no models")
        self.model = model
        return model

    async def _request(self, method: str, url: str, **kwargs) -> httpx2.Response:
        headers = self._auth() if url.startswith(self.base_url + "/") else {}  # the key goes only to the API
        try:
            return await self._http.request(method, url, headers=headers, **kwargs)
        except httpx2.HTTPError as exc:
            raise MusicError(f"acemusic unreachable: {exc}") from exc

    def _auth(self) -> dict[str, str]:
        key = self._key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _forget_old_jobs(self) -> None:
        now = time.monotonic()
        for task_id, job in list(self._jobs.items()):
            if job.finished_at is not None and now - job.finished_at > JOB_TTL:
                if not job.task.cancelled():
                    job.task.exception()  # mark a failure as seen before dropping it
                del self._jobs[task_id]


async def _read_stream(response: httpx2.Response) -> tuple[str | None, str]:
    """SSE: `data: {chunk}` lines until `data: [DONE]`; the audio comes in one chunk's delta."""
    audio_url, text = None, []
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        data = line[len("data: "):].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text.append(delta.get("content") or "")
            audio_url = audio_url or _audio_in(delta)
    return audio_url, "".join(text)


def _read_message(body: dict) -> tuple[str | None, str]:
    """A plain (non-streamed) chat completion."""
    choices = body.get("choices") or []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") or {}
    return _audio_in(message), message.get("content") or ""


def _audio_in(part: dict) -> str | None:
    """The first {"audio": [{"type": "audio_url", "audio_url": {"url": ...}}]} entry."""
    for item in part.get("audio") or []:
        url = (item.get("audio_url") or {}).get("url") if isinstance(item, dict) else None
        if url:
            return url
    return None


def _audio_link(text: str) -> str | None:
    """A link to the audio in the text answer, preferring one that names an audio file."""
    links = _LINK.findall(text or "")
    audio = [link for link in links if PurePosixPath(urlparse(link).path).suffix.lower() in AUDIO_EXTENSIONS]
    return (audio or links or [None])[0]


def _decode_data_url(url: str) -> bytes:
    header, _, data = url.partition(",")
    if not header.endswith(";base64"):
        raise MusicError("the audio data is not base64")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MusicError(f"broken audio data: {exc}") from exc


def _json(response: httpx2.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise MusicError(f"acemusic answered with non-JSON: {response.text[:200]}") from exc
    return body if isinstance(body, dict) else {}


def _detail(response: httpx2.Response) -> str:
    """The API's error text: {"detail": "..."} or OpenAI-style {"error": {"message": "..."}}."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if not isinstance(body, dict):
        return response.text[:200]
    error = body.get("error")
    return str(body.get("detail") or (error.get("message") if isinstance(error, dict) else error) or response.text[:200])

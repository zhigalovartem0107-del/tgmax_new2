"""Song generation via an ACE-Step 1.5 API server (https://github.com/ace-step/ACE-Step-1.5).

The server is asynchronous: POST /release_task returns a task id, POST /query_result
reports its status, and the finished MP3 is downloaded from /v1/audio. Every JSON
response is wrapped as {"data": ..., "code": 200, "error": null}.
"""

import json
from dataclasses import dataclass
from enum import StrEnum

import httpx2

MAX_AUDIO_BYTES = 50 * 1024 * 1024


class MusicError(Exception):
    """The music server failed or answered something unusable."""


class MusicStatus(StrEnum):
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class MusicResult:
    status: MusicStatus
    audio_url: str | None = None
    error: str | None = None


class MusicService:
    def __init__(
        self,
        api_url: str | None,
        api_key: str | None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/") if api_url else None
        self._auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = http_client or httpx2.AsyncClient(timeout=60.0)

    async def generate_song(self, lyrics: str, style: str) -> str:
        """Submit a generation job for these lyrics and return the server's task id."""
        data = await self._call(
            "/release_task",
            {
                "prompt": style,
                "lyrics": lyrics,
                "vocal_language": "ru",
                "audio_format": "mp3",
                "batch_size": 1,
            },
        )
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id:
            raise MusicError(f"no task_id in the music server's answer: {data!r}")
        return task_id

    async def get_result(self, task_id: str) -> MusicResult:
        data = await self._call("/query_result", {"task_id_list": [task_id]})
        item = next((i for i in data or [] if i.get("task_id") == task_id), None)
        if item is None or item.get("status") == 0:  # queued, running, or unknown to the server
            return MusicResult(MusicStatus.PROCESSING)

        outputs = _parse_outputs(item.get("result"))
        if item.get("status") == 1:
            file = next((o["file"] for o in outputs if o.get("file")), None)
            if file is None:
                return MusicResult(MusicStatus.FAILED, error="no audio in the finished task")
            return MusicResult(MusicStatus.DONE, audio_url=self._absolute(file))

        error = next((o["error"] for o in outputs if o.get("error")), None)
        return MusicResult(MusicStatus.FAILED, error=error or "generation failed")

    async def download(self, audio_url: str) -> bytes:
        """Fetch the finished audio. The API key goes only to the music server itself."""
        own_server = self.api_url is not None and audio_url.startswith(self.api_url + "/")
        response = await self._request("GET", audio_url, headers=self._auth if own_server else {})
        if len(response.content) > MAX_AUDIO_BYTES:
            raise MusicError(f"audio is too large: {len(response.content)} bytes")
        return response.content

    async def close(self) -> None:
        await self._http.aclose()

    def _absolute(self, file: str) -> str:
        return file if file.startswith(("http://", "https://")) else f"{self.api_url}{file}"

    async def _call(self, path: str, payload: dict):
        if not self.api_url:
            raise MusicError("MUSIC_API_URL is not set")
        response = await self._request("POST", self.api_url + path, json=payload, headers=self._auth)
        try:
            body = response.json()
        except ValueError as exc:
            raise MusicError(f"music server answered with non-JSON: {response.text[:200]}") from exc
        if body.get("code") != 200:
            raise MusicError(f"music server error {body.get('code')}: {body.get('error')}")
        return body.get("data")

    async def _request(self, method: str, url: str, **kwargs) -> httpx2.Response:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise MusicError(f"music server unreachable: {exc}") from exc
        if response.status_code != 200:
            raise MusicError(f"music server HTTP {response.status_code}: {response.text[:200]}")
        return response


def _parse_outputs(result) -> list[dict]:
    """`result` is a JSON string holding a list of output descriptions."""
    try:
        outputs = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return []
    return [o for o in outputs or [] if isinstance(o, dict)]

"""AIService against a mocked OpenRouter HTTP API (real openai SDK, fake transport)."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx2
import pytest

from config import Settings
from services.ai import DEFAULT_MODELS, AIService, LyricsError, LyricsRateLimited

LYRICS = "[Куплет 1]\nКот гуляет по крыше ночной\n\n[Припев]\nМяу, мяу, луна надо мной"


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _model(request: httpx2.Request) -> str:
    return json.loads(request.content)["model"]


def _completion(content: str | None, finish_reason: str = "stop") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1,
            "model": "vendor/model:free",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
        },
    )


def _rate_limited(headers: dict | None = None, **error) -> httpx2.Response:
    """OpenRouter's 429: {"error": {"message", "code", "metadata"}}."""
    body = {"message": "Rate limit exceeded: free-models-per-min.", "code": 429, **error}
    return httpx2.Response(429, headers=headers or {}, json={"error": body})


async def test_write_lyrics_returns_string():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _completion(f"\n{LYRICS}\n")

    service = AIService("test-key", models=["vendor/model:free"], http_client=_client(handler))
    lyrics = await service.write_lyrics("песня про кота", "Рок")
    await service.close()

    assert isinstance(lyrics, str)
    assert lyrics == LYRICS

    [request] = requests
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "vendor/model:free"
    system, user = body["messages"]
    assert system["role"] == "system" and "lyrics in Russian" in system["content"]
    assert user["role"] == "user"
    assert "Music style: Рок" in user["content"] and "песня про кота" in user["content"]


async def test_reasoning_block_is_not_part_of_lyrics():
    service = AIService(
        "test-key", http_client=_client(lambda r: _completion(f"<think>rhymes first</think>\n{LYRICS}"))
    )
    assert await service.write_lyrics("о море", "Поп") == LYRICS
    await service.close()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx2.Response(401, json={"error": {"message": "No auth credentials found", "code": 401}}),
            "401",
        ),
        (_completion("[Куплет 1]\nКот гуля", finish_reason="length"), "cut off"),
        (_completion(None, finish_reason="error"), "no lyrics"),
        (
            httpx2.Response(200, json={"id": "gen-1", "object": "chat.completion", "created": 1, "model": "x", "choices": []}),
            "no lyrics",
        ),
    ],
    ids=["api-error", "cut-off", "empty", "no-choices"],
)
async def test_failures_raise_lyrics_error(response, message):
    service = AIService("test-key", http_client=_client(lambda request: response))
    with pytest.raises(LyricsError, match=message):
        await service.write_lyrics("песня про кота", "Рок")
    await service.close()


def test_defaults_are_specific_fast_free_models(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    for name in ("OPENROUTER_MODEL", "OPENROUTER_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)

    service = AIService.from_settings(Settings(_env_file=None))

    assert service.models == list(DEFAULT_MODELS)
    first, *_, last = service.models
    assert first.endswith(":free") and first != "openrouter/free"  # a specific model, not the free router
    assert last == "openrouter/free"  # the last resort: whichever free model is up
    assert service.timeout == 30


async def test_models_and_timeout_come_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("OPENROUTER_MODEL", " vendor/a:free , vendor/b:free ")
    monkeypatch.setenv("OPENROUTER_TIMEOUT", "12")
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.headers["authorization"], _model(request)))
        return _completion(LYRICS)

    service = AIService.from_settings(Settings(_env_file=None), http_client=_client(handler))
    assert (service.models, service.timeout) == (["vendor/a:free", "vendor/b:free"], 12)
    assert await service.write_lyrics("о море", "Поп") == LYRICS
    await service.close()
    assert seen == [("Bearer env-key", "vendor/a:free")]


async def test_slow_model_is_dropped_for_the_next_one():
    seen = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(_model(request))
        if _model(request) == "slow/model:free":
            await asyncio.sleep(30)  # an overloaded free model
        return _completion(LYRICS)

    service = AIService("k", models=["slow/model:free", "fast/model:free"], timeout=0.2, http_client=_client(handler))
    lyrics = await asyncio.wait_for(service.write_lyrics("о море", "Поп"), timeout=3)
    await service.close()

    assert lyrics == LYRICS
    assert seen == ["slow/model:free", "fast/model:free"]


async def test_no_model_answering_in_time_is_a_clear_error():
    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(30)

    service = AIService("k", models=["a/m:free", "b/m:free"], timeout=0.1, http_client=_client(handler))
    with pytest.raises(LyricsError, match=r"a/m:free: no answer in 0.1 s; b/m:free: no answer in 0.1 s"):
        await asyncio.wait_for(service.write_lyrics("о море", "Поп"), timeout=3)
    await service.close()


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: httpx2.Response(503, json={"error": {"message": "overloaded", "code": 503}}),
        lambda request: httpx2.Response(404, json={"error": {"message": "No endpoints found", "code": 404}}),
        lambda request: _completion(None, finish_reason="error"),
        lambda request: (_ for _ in ()).throw(httpx2.ConnectError("connection refused", request=request)),
    ],
    ids=["503", "no-endpoints", "empty", "connection-error"],
)
async def test_failed_model_falls_back_without_hidden_retries(failure):
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(_model(request))
        return failure(request) if _model(request) == "down/model:free" else _completion(LYRICS)

    service = AIService("k", models=["down/model:free", "up/model:free"], http_client=_client(handler))
    assert await service.write_lyrics("о море", "Поп") == LYRICS
    await service.close()

    assert seen == ["down/model:free", "up/model:free"]  # one request each: no retries in the background


async def test_rejected_key_is_not_tried_on_other_models():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(_model(request))
        return httpx2.Response(401, json={"error": {"message": "No auth credentials found", "code": 401}})

    service = AIService("k", models=["a/m:free", "b/m:free"], http_client=_client(handler))
    with pytest.raises(LyricsError, match="401"):
        await service.write_lyrics("о море", "Поп")
    await service.close()
    assert seen == ["a/m:free"]


async def test_rate_limited_model_falls_back_to_the_next():
    def handler(request: httpx2.Request) -> httpx2.Response:
        if _model(request) == "busy/model:free":
            return _rate_limited({"Retry-After": "60"}, message="busy/model:free is temporarily rate-limited upstream")
        return _completion(LYRICS)

    service = AIService("k", models=["busy/model:free", "free/model:free"], http_client=_client(handler))
    assert await asyncio.wait_for(service.write_lyrics("о море", "Поп"), timeout=3) == LYRICS
    await service.close()


def _in(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@pytest.mark.parametrize(
    ("response", "wait"),
    [
        (lambda: _rate_limited({"Retry-After": "120"}), 120),
        (lambda: _rate_limited({"Retry-After": format_datetime(_in(300), usegmt=True)}), 300),
        (lambda: _rate_limited({"retry-after-ms": "30000"}), 30),
        (lambda: _rate_limited({"X-RateLimit-Reset": str(int((time.time() + 600) * 1000))}), 600),
        (  # OpenRouter's own limits put the headers into the error body
            lambda: _rate_limited(metadata={"headers": {"X-RateLimit-Reset": str(int((time.time() + 600) * 1000))}}),
            600,
        ),
        (lambda: _rate_limited(), None),
    ],
    ids=["retry-after", "retry-after-date", "retry-after-ms", "ratelimit-reset", "openrouter-metadata", "unknown"],
)
async def test_rate_limit_tells_when_to_retry_at_once(response, wait):
    """The SDK used to sleep on Retry-After (up to 2 minutes per retry) while the user waited."""
    service = AIService("k", models=["a/m:free", "b/m:free"], http_client=_client(lambda request: response()))

    with pytest.raises(LyricsRateLimited) as caught:
        await asyncio.wait_for(service.write_lyrics("о море", "Поп"), timeout=3)
    await service.close()

    if wait is None:
        assert caught.value.retry_after is None
    else:
        assert caught.value.retry_after == pytest.approx(wait, abs=5)


def test_missing_openrouter_api_key_is_rejected(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        AIService.from_settings(Settings(_env_file=None))

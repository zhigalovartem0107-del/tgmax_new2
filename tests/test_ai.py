"""AIService against a mocked OpenRouter HTTP API (real openai SDK, fake transport)."""

import json

import httpx2
import pytest

from config import Settings
from services.ai import AIService, LyricsError

LYRICS = "[Куплет 1]\nКот гуляет по крыше ночной\n\n[Припев]\nМяу, мяу, луна надо мной"


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


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


async def test_write_lyrics_returns_string():
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return _completion(f"\n{LYRICS}\n")

    service = AIService("test-key", model="vendor/model:free", http_client=_client(handler))
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


async def test_key_and_model_come_from_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/model:free")
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.headers["authorization"], json.loads(request.content)["model"]))
        return _completion(LYRICS)

    service = AIService.from_settings(Settings(_env_file=None), http_client=_client(handler))
    assert await service.write_lyrics("о море", "Поп") == LYRICS
    await service.close()
    assert seen == [("Bearer env-key", "vendor/model:free")]


def test_missing_openrouter_api_key_is_rejected(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        AIService.from_settings(Settings(_env_file=None))

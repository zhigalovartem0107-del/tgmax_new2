"""GeminiService against a mocked Gemini HTTP API (real SDK, fake transport)."""

import json

import httpx
import pytest
from google.genai import types

from config import Settings
from services.ai import DEFAULT_MODEL, GeminiService, LyricsError

LYRICS = "[Куплет 1]\nКот гуляет по крыше ночной\n\n[Припев]\nМяу, мяу, луна надо мной"


def _options(handler) -> types.HttpOptions:
    return types.HttpOptions(async_client_args={"transport": httpx.MockTransport(handler)})


def _ok(text: str, finish_reason: str = "STOP") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": finish_reason,
                    "index": 0,
                }
            ]
        },
    )


async def test_write_lyrics_returns_string():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ok(f"\n{LYRICS}\n")

    service = GeminiService("test-key", http_options=_options(handler))
    lyrics = await service.write_lyrics("песня про кота", "Рок")
    await service.close()

    assert isinstance(lyrics, str)
    assert lyrics == LYRICS

    [request] = requests
    assert request.url.path.endswith(f"/models/{DEFAULT_MODEL}:generateContent")
    assert request.headers["x-goog-api-key"] == "test-key"
    body = json.loads(request.content)
    assert "lyrics in Russian" in body["systemInstruction"]["parts"][0]["text"]
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "Music style: Рок" in prompt and "песня про кота" in prompt


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": "User location is not supported for the API use.",
                        "status": "FAILED_PRECONDITION",
                    }
                },
            ),
            "User location is not supported",
        ),
        (httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}), "SAFETY"),
        (_ok("[Куплет 1]\nКот гуля", finish_reason="MAX_TOKENS"), "MAX_TOKENS"),
    ],
    ids=["api-error", "blocked", "cut-off"],
)
async def test_failures_raise_lyrics_error(response, message):
    service = GeminiService("test-key", http_options=_options(lambda request: response))
    with pytest.raises(LyricsError, match=message):
        await service.write_lyrics("песня про кота", "Рок")
    await service.close()


async def test_api_key_comes_from_gemini_api_key_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["x-goog-api-key"])
        return _ok(LYRICS)

    service = GeminiService.from_settings(Settings(_env_file=None), http_options=_options(handler))
    assert await service.write_lyrics("о море", "Поп") == LYRICS
    await service.close()
    assert keys == ["env-key"]


def test_missing_gemini_api_key_is_rejected(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiService.from_settings(Settings(_env_file=None))

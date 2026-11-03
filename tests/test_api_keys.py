"""API keys an admin manages in the bot: stored in the database, winning over environment variables."""

import asyncio

import httpx2
import pytest
from conftest import USER_ID, Sent
from maxapi import Bot
from maxapi.methods.types.deleted_message import DeletedMessage
from pydantic import SecretStr

from config import Settings, settings
from database import session_factory
from keyboards.admin import admin_menu_kb, keys_kb
from services.keys import ApiKeys, mask
from services.music import MusicService, MusicStatus

KEY = "sk-live-1234567890abcd"
SONG_STREAM = (  # a minimal acemusic answer with audio
    b'data: {"choices":[{"index":0,"delta":{"audio":[{"type":"audio_url",'
    b'"audio_url":{"url":"data:audio/mpeg;base64,SUQz"}}]},"finish_reason":null}]}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", [USER_ID])


@pytest.fixture
def no_env_key(monkeypatch):
    for name in ("ACEMUSIC_API_KEY", "MUSIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


async def _keys(cfg: Settings) -> ApiKeys:
    keys = ApiKeys(session_factory, cfg)
    await keys.load()
    return keys


async def test_key_saved_in_the_bot_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("ACEMUSIC_API_KEY", "env-key")
    keys = await _keys(Settings(_env_file=None))
    assert keys.get("ACEMUSIC_API_KEY") == "env-key"

    await keys.set("ACEMUSIC_API_KEY", "bot-key")
    assert keys.get("ACEMUSIC_API_KEY") == "bot-key"

    await keys.clear("ACEMUSIC_API_KEY")
    assert keys.get("ACEMUSIC_API_KEY") == "env-key"


async def test_saved_keys_survive_a_restart(no_env_key):
    cfg = Settings(_env_file=None)
    await (await _keys(cfg)).set("ACEMUSIC_API_KEY", "bot-key")

    restarted = await _keys(cfg)
    assert restarted.get("ACEMUSIC_API_KEY") == "bot-key"


async def test_no_key_anywhere_is_none(no_env_key):
    assert (await _keys(Settings(_env_file=None))).get("ACEMUSIC_API_KEY") is None


@pytest.mark.parametrize(("value", "shown"), [(KEY, "••••abcd"), ("short", "••••")])
def test_keys_are_masked(value, shown):
    assert mask(value) == shown


async def test_music_service_uses_a_new_key_at_once(no_env_key):
    keys = await _keys(Settings(_env_file=None))
    await keys.set("ACEMUSIC_API_KEY", "first-key")
    sent_with = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent_with.append(request.headers["authorization"])
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=SONG_STREAM)

    music = MusicService(
        keys.getter("ACEMUSIC_API_KEY"), model="m", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    )
    for new_key in (None, "second-key"):
        if new_key:
            await keys.set("ACEMUSIC_API_KEY", new_key)  # no restart in between
        task_id = await music.generate_song(lyrics="x", style="Поп")
        while (await music.get_result(task_id)).status == MusicStatus.PROCESSING:
            await asyncio.sleep(0.01)
    await music.close()

    assert sent_with == ["Bearer first-key", "Bearer second-key"]


async def test_admin_panel_opens_the_api_keys(bot, admin):
    await bot.text("/admin")
    assert "🔑 API Ключи" in bot.last.buttons

    await bot.tap("🔑 API Ключи")
    assert "ACEMUSIC_API_KEY" in bot.last.text and "не задан" in bot.last.text


async def test_admin_saves_a_key_and_it_is_used_at_once(bot, admin):
    await bot.text("/admin")
    await bot.tap("🔑 API Ключи")
    await bot.tap("✏️ ACE Music")
    assert "ACEMUSIC_API_KEY" in bot.last.text  # asks for the value

    await bot.text(f"  {KEY}​\n")  # as pasted: spaces, a zero-width character, a newline

    assert bot.keys.get("ACEMUSIC_API_KEY") == KEY
    assert bot.music_key() == KEY  # the running MusicService sees it
    assert bot.deleted == ["mid.1"]  # the message with the key is removed from the chat
    assert all(KEY not in (s.text or "") for s in bot.sent)  # never echoed back
    assert "••••abcd" in bot.last.text
    assert (await _keys(settings)).get("ACEMUSIC_API_KEY") == KEY  # stored in the database


@pytest.mark.parametrize("failure", [DeletedMessage(success=False, message="no rights"), RuntimeError("403")])
async def test_admin_is_warned_when_the_key_stays_in_the_chat(bot, admin, monkeypatch, failure):
    async def delete_message(self, message_id, *a, **kw):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(Bot, "delete_message", delete_message)
    await bot.text("/admin")
    await bot.tap("🔑 API Ключи")
    await bot.tap("✏️ ACE Music")

    await bot.text(KEY)

    assert bot.keys.get("ACEMUSIC_API_KEY") == KEY
    assert "удали его сам" in bot.last.text


async def test_admin_removes_a_key_and_the_environment_one_is_used(bot, admin, monkeypatch):
    monkeypatch.setattr(settings, "music_api_key", SecretStr("env-key-0000wxyz"))
    await bot.keys.set("ACEMUSIC_API_KEY", KEY)
    await bot.text("/admin")
    await bot.tap("🔑 API Ключи")

    await bot.tap("🗑 ACE Music")

    assert bot.keys.get("ACEMUSIC_API_KEY") == "env-key-0000wxyz"
    assert "••••wxyz" in bot.last.text and "окружения" in bot.last.text


@pytest.mark.parametrize("not_a_key", ["   ", "/shop"])
async def test_empty_value_or_a_command_is_not_saved(bot, admin, not_a_key):
    await bot.text("/admin")
    await bot.tap("🔑 API Ключи")
    await bot.tap("✏️ ACE Music")

    await bot.text(not_a_key)
    assert bot.keys.get("ACEMUSIC_API_KEY") is None

    await bot.text(KEY)  # still waiting for the key
    assert bot.keys.get("ACEMUSIC_API_KEY") == KEY


async def test_cancel_stops_waiting_for_the_key(bot, admin):
    await bot.text("/admin")
    await bot.tap("🔑 API Ключи")
    await bot.tap("✏️ ACE Music")

    await bot.text("/cancel")
    await bot.text(KEY)
    assert bot.keys.get("ACEMUSIC_API_KEY") is None


async def test_regular_user_cannot_see_or_change_keys(bot):
    """Buttons from an admin's message (e.g. forwarded) don't work for anyone else."""
    await bot.keys.set("ACEMUSIC_API_KEY", KEY)
    keys_panel = Sent("🔑 API ключи", [keys_kb(bot.keys)])
    await bot.start()

    await bot.tap("🔑 API Ключи", Sent("📊 Статистика", [admin_menu_kb()]))
    assert "⛔" in (bot.last.notification or "")
    for button in ("✏️ ACE Music", "🗑 ACE Music"):
        await bot.tap(button, keys_panel)
        assert "⛔" in (bot.last.notification or "")

    await bot.text("a-stranger-key")
    assert bot.keys.get("ACEMUSIC_API_KEY") == KEY

"""Test harness: a real Dispatcher fed with MAX-format updates; only HTTP calls are stubbed."""

import itertools
import os
import tempfile
from dataclasses import dataclass
from typing import Any

# Must be set before config is imported.
_TMP = tempfile.mkdtemp(prefix="maxbot-tests-")
os.environ["MAX_BOT_TOKEN"] = "test-token"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP}/test.db"
os.environ["SONG_PRICE"] = "10"
os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
os.environ["TERMS_URL"] = "https://example.com/terms"
os.environ["PRIVACY_URL"] = "https://example.com/privacy"
os.environ["SHOP_PACKAGES"] = "5:199,15:499"

import pytest  # noqa: E402
from maxapi import Bot  # noqa: E402
from maxapi.methods.types.deleted_message import DeletedMessage  # noqa: E402
from maxapi.methods.types.getted_updates import process_update_request  # noqa: E402
from maxapi.types import User as MaxUser  # noqa: E402

import app  # noqa: E402
from config import settings  # noqa: E402
from database import Base, close_db, engine, session_factory  # noqa: E402
from services.ai import AIService  # noqa: E402
from services.keys import ApiKeys  # noqa: E402
from services.music import MusicService  # noqa: E402

USER_ID = 777
CHAT_ID = 555
USER = {
    "user_id": USER_ID,
    "first_name": "Ivan",
    "last_name": None,
    "username": "ivan",
    "is_bot": False,
    "last_activity_time": 0,
}


def _message(text: str) -> dict[str, Any]:
    return {
        "sender": USER,
        "recipient": {"chat_id": CHAT_ID, "chat_type": "dialog", "user_id": USER_ID},
        "timestamp": 1,
        "body": {"mid": "mid.1", "seq": 1, "text": text},
    }


@dataclass
class Sent:
    text: str | None
    attachments: list | None
    notification: str | None = None
    user_id: int | None = None  # recipient of a send_message(user_id=...)

    @property
    def buttons(self) -> dict[str, str]:
        """Button text -> callback payload (link buttons: their url)."""
        result = {}
        for attachment in self.attachments or []:
            for row in attachment.payload.buttons:
                for button in row:
                    result[button.text] = getattr(button, "payload", None) or getattr(button, "url", None)
        return result


class FakeYooKassa:
    """Stands in for services.yookassa.YooKassaService: payments live in memory."""

    configured = True

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.status: dict[str, str] = {}
        self.fail: Exception | None = None
        self.status_fail: Exception | None = None

    async def create_payment(self, amount_kopecks, description, email=None, metadata=None):
        if self.fail:
            raise self.fail
        pid = f"pay-{len(self.created) + 1}"
        self.created.append({"id": pid, "amount_kopecks": amount_kopecks, "description": description,
                             "email": email, "metadata": metadata})
        self.status[pid] = "pending"
        return pid, f"https://yoomoney.ru/checkout/{pid}"

    async def get_status(self, pid):
        if self.status_fail:
            raise self.status_fail
        return self.status.get(pid, "pending")

    async def close(self):
        pass


class BotHarness:
    def __init__(self, bot: Bot, dp, sent: list[Sent], yookassa: FakeYooKassa) -> None:
        self.bot, self.dp, self.sent, self.yookassa = bot, dp, sent, yookassa
        self.deleted: list[str] = []  # message ids the bot deleted
        self.keys: ApiKeys | None = None
        self.music_key = lambda: None  # the key the running MusicService would send now
        self._ids = itertools.count(1)

    @property
    def last(self) -> Sent:
        return self.sent[-1]

    async def _feed(self, update: dict[str, Any]) -> None:
        events = await process_update_request({"updates": [update]}, self.bot)
        assert len(events) == 1, f"update not parsed: {update}"
        await self.dp.handle(events[0])

    async def start(self) -> None:
        await self._feed(
            {"update_type": "bot_started", "timestamp": 1, "chat_id": CHAT_ID, "user": USER}
        )

    async def text(self, text: str) -> None:
        await self._feed({"update_type": "message_created", "timestamp": 1, "message": _message(text)})

    async def tap(self, button_text: str, message: Sent | None = None) -> None:
        """Press a button by its label on `message` (default: the last message sent)."""
        payload = (message or self.last).buttons[button_text]
        await self._feed(
            {
                "update_type": "message_callback",
                "timestamp": 1,
                "message": _message("menu"),
                "callback": {
                    "timestamp": 1,
                    "callback_id": f"cb{next(self._ids)}",
                    "payload": payload,
                    "user": USER,
                },
            }
        )


@pytest.fixture(autouse=True)
async def db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await close_db()  # connections are bound to this test's event loop


@pytest.fixture
async def session():
    async with session_factory() as s:
        yield s


@pytest.fixture
async def bot(monkeypatch) -> BotHarness:
    sent: list[Sent] = []

    async def send_message(self, chat_id=None, user_id=None, text=None, attachments=None, *a, **kw):
        sent.append(Sent(text, attachments, user_id=user_id))

    async def send_callback(self, callback_id, message=None, notification=None, *a, **kw):
        sent.append(
            Sent(
                message.text if message else None,
                message.attachments if message else None,
                notification,
            )
        )

    async def send_action(self, chat_id=None, action=None, *a, **kw):
        return None  # "typing…" indicator

    async def get_me(self):
        return MaxUser(user_id=1, first_name="Bot", username="bot", is_bot=True, last_activity_time=0)

    deleted: list[str] = []

    async def delete_message(self, message_id, *a, **kw):
        deleted.append(message_id)
        return DeletedMessage(success=True)

    monkeypatch.setattr(Bot, "send_message", send_message)
    monkeypatch.setattr(Bot, "send_callback", send_callback)
    monkeypatch.setattr(Bot, "send_action", send_action)
    monkeypatch.setattr(Bot, "get_me", get_me)
    monkeypatch.setattr(Bot, "delete_message", delete_message)

    max_bot = Bot(token="test-token", auto_requests=False)
    ai = AIService.from_settings(settings)
    keys = ApiKeys(session_factory, settings)
    await keys.load()
    music = MusicService(keys.getter("ACEMUSIC_API_KEY"))
    yookassa = FakeYooKassa()
    dp = app.build_dispatcher(ai, music, yookassa, keys)
    await dp.startup(max_bot)  # same preparation start_polling does
    harness = BotHarness(max_bot, dp, sent, yookassa)
    harness.deleted, harness.keys, harness.music_key = deleted, keys, music._key
    yield harness
    await max_bot.close_session()
    await ai.close()
    await music.close()

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

import pytest  # noqa: E402
from maxapi import Bot  # noqa: E402
from maxapi.methods.types.getted_updates import process_update_request  # noqa: E402
from maxapi.types import User as MaxUser  # noqa: E402

import main  # noqa: E402
from database import Base, close_db, engine, session_factory  # noqa: E402

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

    @property
    def buttons(self) -> dict[str, str]:
        """Button text -> callback payload."""
        result = {}
        for attachment in self.attachments or []:
            for row in attachment.payload.buttons:
                for button in row:
                    result[button.text] = button.payload
        return result


class BotHarness:
    def __init__(self, bot: Bot, dp, sent: list[Sent]) -> None:
        self.bot, self.dp, self.sent = bot, dp, sent
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
        sent.append(Sent(text, attachments))

    async def send_callback(self, callback_id, message=None, notification=None, *a, **kw):
        sent.append(
            Sent(
                message.text if message else None,
                message.attachments if message else None,
                notification,
            )
        )

    async def get_me(self):
        return MaxUser(user_id=1, first_name="Bot", username="bot", is_bot=True, last_activity_time=0)

    monkeypatch.setattr(Bot, "send_message", send_message)
    monkeypatch.setattr(Bot, "send_callback", send_callback)
    monkeypatch.setattr(Bot, "get_me", get_me)

    max_bot = Bot(token="test-token", auto_requests=False)
    dp = main.build_dispatcher()
    await dp.startup(max_bot)  # same preparation start_polling does
    yield BotHarness(max_bot, dp, sent)
    await max_bot.close_session()

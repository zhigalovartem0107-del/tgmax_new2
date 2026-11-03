"""API keys an admin sets in the bot. A key saved in the bot wins over the environment variable.

The saved keys live in the api_keys table and in memory: get() is instant, and a key saved
from the admin panel is used by the next request, without a restart.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config import Settings
from database.models import ApiKey


@dataclass(frozen=True)
class KeySpec:
    name: str  # the environment variable, e.g. ACEMUSIC_API_KEY
    title: str  # shown on the admin panel buttons
    env: Callable[[Settings], object]  # reads the environment value from the settings


KEYS = (
    KeySpec("ACEMUSIC_API_KEY", "ACE Music", lambda s: s.music_api_key),  # MUSIC_API_KEY works too
    KeySpec("VK_TOKEN", "VK токен", lambda s: s.vk_token),
    KeySpec("VK_COOKIES", "VK cookies", lambda s: s.vk_cookies),
    KeySpec("TIKTOK_COOKIES", "TikTok cookies", lambda s: s.tiktok_cookies),
)

# What copy-paste adds invisibly: zero-width characters and BOM.
_INVISIBLE = re.compile("[​-‏⁠﻿]")


def mask(value: str) -> str:
    """Only the last 4 characters, and none of a short value."""
    return f"••••{value[-4:]}" if len(value) >= 8 else "••••"


def clean(value: str) -> str:
    """The key as pasted, without invisible characters, surrounding spaces and quotes."""
    return _INVISIBLE.sub("", value).strip().strip("\"'«»`").strip()


class ApiKeys:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cfg: Settings,
        specs: tuple[KeySpec, ...] = KEYS,
    ) -> None:
        self._session_factory = session_factory
        self._cfg = cfg
        self.specs = {spec.name: spec for spec in specs}
        self._saved: dict[str, str] = {}

    async def load(self) -> None:
        """Read the keys saved in the bot; call once at startup."""
        async with self._session_factory() as session:
            rows = (await session.scalars(select(ApiKey))).all()
        self._saved = {row.name: row.value for row in rows if row.name in self.specs}

    def get(self, name: str) -> str | None:
        """The key saved in the bot, else the environment variable, else None."""
        return self._saved.get(name) or self.env_value(name)

    def env_value(self, name: str) -> str | None:
        value = self.specs[name].env(self._cfg)
        value = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        return value or None

    def saved_in_bot(self, name: str) -> bool:
        return name in self._saved

    def getter(self, name: str) -> Callable[[], str | None]:
        """For services that must see a newly saved key on their next request."""
        self.specs[name]  # unknown names fail here, not on the first request
        return lambda: self.get(name)

    async def set(self, name: str, value: str, admin_id: int | None = None) -> None:
        self.specs[name]
        async with self._session_factory() as session:
            await session.merge(ApiKey(name=name, value=value, updated_by=admin_id))
            await session.commit()
        self._saved[name] = value

    async def clear(self, name: str) -> None:
        self.specs[name]
        async with self._session_factory() as session:
            await session.execute(delete(ApiKey).where(ApiKey.name == name))
            await session.commit()
        self._saved.pop(name, None)

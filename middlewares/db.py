from typing import Any

from maxapi.filters.middleware import BaseMiddleware, HandlerCallable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """One session (unit of work) per update: commit on success, roll back on error.

    Handlers receive it by declaring a `session: AsyncSession` parameter.
    Long-running jobs (waiting for a song or a video) must open their own
    session instead of holding this one open.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(self, handler: HandlerCallable, event_object: Any, data: dict[str, Any]) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            result = await handler(event_object, data)
            await session.commit()
            return result

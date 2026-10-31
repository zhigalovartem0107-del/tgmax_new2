from typing import Any

from maxapi.filters.middleware import BaseMiddleware, HandlerCallable
from maxapi.types import BotStarted, MessageCallback, MessageCreated
from maxapi.types import User as MaxUser

from database import crud


def _event_user(event: Any) -> MaxUser | None:
    if isinstance(event, MessageCreated):
        return event.message.sender
    if isinstance(event, MessageCallback):
        return event.callback.user
    if isinstance(event, BotStarted):
        return event.user
    user = getattr(event, "user", None)
    return user if isinstance(user, MaxUser) else None


class UserMiddleware(BaseMiddleware):
    """Registers or refreshes the sender and passes it to handlers as `db_user`.

    Must run after DbSessionMiddleware. Updates from banned users are dropped.
    """

    async def __call__(self, handler: HandlerCallable, event_object: Any, data: dict[str, Any]) -> Any:
        max_user = _event_user(event_object)
        if max_user is None or max_user.is_bot:
            return await handler(event_object, data)

        db_user, _created = await crud.get_or_create_user(
            data["session"],
            user_id=max_user.user_id,
            first_name=max_user.first_name,
            last_name=max_user.last_name,
            username=max_user.username,
        )
        if db_user.is_banned:
            return None

        data["db_user"] = db_user
        return await handler(event_object, data)

from typing import Any

from maxapi.filters.middleware import BaseMiddleware, HandlerCallable


class ServicesMiddleware(BaseMiddleware):
    """Passes shared service clients to handlers by name, e.g. a `music` parameter."""

    def __init__(self, **services: Any) -> None:
        self.services = services

    async def __call__(self, handler: HandlerCallable, event_object: Any, data: dict[str, Any]) -> Any:
        data.update(self.services)
        return await handler(event_object, data)

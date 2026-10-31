"""YooKassa payments.

Calls the REST API (https://api.yookassa.ru/v3) through aiohttp: the official
`yookassa` SDK is synchronous and would block the bot's event loop.
"""


class YooKassaService:
    def __init__(self, shop_id: str | None, secret_key: str | None) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key

    async def create_payment(self, amount_kopecks: int, description: str, user_id: int) -> str:
        """Create a payment and return the confirmation URL for the user."""
        raise NotImplementedError

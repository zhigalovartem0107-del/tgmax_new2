"""YooKassa payments.

Calls the REST API (https://api.yookassa.ru/v3) through aiohttp: the official
`yookassa` SDK is synchronous and would block the bot's event loop.
"""

import base64
import uuid

import aiohttp

API_URL = "https://api.yookassa.ru"


class YooKassaError(Exception):
    """YooKassa answered with an error. Keeps the exact HTTP status and body for the logs."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"YooKassa {status}: {body}")
        self.status = status
        self.body = body


class YooKassaService:
    def __init__(
        self,
        shop_id: str | None,
        secret_key: str | None,
        receipt_email: str | None = None,
        base_url: str = API_URL,
        return_url: str = "https://max.ru",
    ) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.receipt_email = receipt_email
        self.base_url = base_url
        self.return_url = return_url
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_settings(cls, settings) -> "YooKassaService":
        secret = settings.yookassa_secret_key.get_secret_value() if settings.yookassa_secret_key else None
        return cls(settings.yookassa_shop_id, secret, settings.yookassa_receipt_email)

    @property
    def configured(self) -> bool:
        return bool(self.shop_id and self.secret_key)

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": "Basic " + base64.b64encode(
                    f"{self.shop_id or ''}:{self.secret_key or ''}".encode()).decode()},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with self._http().request(method, self.base_url + path, **kwargs) as response:
            text = await response.text()
            if response.status >= 300:
                raise YooKassaError(response.status, text)
            return await response.json(content_type=None)

    async def create_payment(
        self, amount_kopecks: int, description: str, email: str | None = None, metadata: dict | None = None
    ) -> tuple[str, str]:
        """Create a payment and return (YooKassa payment id, confirmation URL for the user).

        The 54-FZ receipt goes to the buyer's email, or to the shop's own email when the buyer has none;
        without either, no receipt is attached (the shop must not require receipts then).
        """
        amount = {"value": f"{amount_kopecks // 100}.{amount_kopecks % 100:02d}", "currency": "RUB"}
        body: dict = {
            "amount": amount,
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": self.return_url},
            "description": description[:128],
            "metadata": metadata or {},
        }
        customer = email or self.receipt_email
        if customer:
            body["receipt"] = {
                "customer": {"email": customer},
                "items": [{
                    "description": description[:128], "quantity": "1.00", "amount": amount, "vat_code": 1,
                    "payment_subject": "service", "payment_mode": "full_payment",
                }],
            }
        data = await self._request(
            "POST", "/v3/payments", json=body, headers={"Idempotence-Key": str(uuid.uuid4())}
        )
        return data["id"], data["confirmation"]["confirmation_url"]

    async def get_status(self, payment_id: str) -> str:
        """pending, waiting_for_capture, succeeded or canceled."""
        data = await self._request("GET", f"/v3/payments/{payment_id}")
        return data["status"]

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

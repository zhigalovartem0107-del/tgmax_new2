"""YooKassa payments.

Calls the REST API (https://api.yookassa.ru/v3) through aiohttp: the official
`yookassa` SDK is synchronous and would block the bot's event loop.
"""

import base64
import logging
import re
import uuid

import aiohttp

API_URL = "https://api.yookassa.ru"
logger = logging.getLogger(__name__)

# What copy-paste into a hosting panel adds invisibly: spaces, newlines, quotes, zero-width characters, BOM.
# A real shopId (digits) and secret key (live_/test_ + letters, digits, _ -) never contain any of these.
_HIDDEN = re.compile(r"[\s\"'«»`\u200b-\u200f\u2060\ufeff]")


def _clean(value: str | None) -> str | None:
    return _HIDDEN.sub("", value) if value else value


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
        self.shop_id = _clean(shop_id)
        self.secret_key = _clean(secret_key)
        # How many invisible characters were cut from each setting — shown in the 401 diagnostics.
        self._removed = (len(shop_id or "") - len(self.shop_id or ""), len(secret_key or "") - len(self.secret_key or ""))
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
            if response.status == 401:
                self._log_auth_failure()
            if response.status >= 300:
                raise YooKassaError(response.status, text)
            return await response.json(content_type=None)

    def _log_auth_failure(self) -> None:
        """What exactly was sent, without revealing the key: compare with the YooKassa dashboard
        (Integration → API keys: shopId, and the key's beginning and last characters)."""
        key = self.secret_key or ""
        kind = "live_" if key.startswith("live_") else "test_" if key.startswith("test_") else "neither live_ nor test_"
        logger.error(
            "YooKassa 401 invalid credentials: shopId=%r%s (removed %d hidden characters from YOOKASSA_SHOP_ID); "
            "secret key: %s, length %d, ends with …%s (removed %d hidden characters from YOOKASSA_SECRET_KEY)",
            self.shop_id, "" if (self.shop_id or "").isdigit() else " — NOT digits only, check YOOKASSA_SHOP_ID",
            self._removed[0], kind, len(key), key[-4:], self._removed[1],
        )

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

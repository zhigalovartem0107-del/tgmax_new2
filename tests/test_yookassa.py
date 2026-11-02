"""YooKassa client against a local fake YooKassa server (real HTTP, no network)."""

import base64
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from services.yookassa import YooKassaError, YooKassaService


@pytest.fixture
async def fake_yookassa():
    calls: list[dict] = []
    state = {"status": "pending", "fail": None}

    async def create(request: web.Request) -> web.Response:
        body = await request.json()
        calls.append({"headers": dict(request.headers), "body": body})
        if state["fail"]:
            return web.json_response(state["fail"], status=401)
        return web.json_response(
            {"id": "pay-1", "status": "pending",
             "confirmation": {"type": "redirect", "confirmation_url": "https://yoomoney.ru/checkout/pay-1"}}
        )

    async def status(request: web.Request) -> web.Response:
        return web.json_response({"id": request.match_info["pid"], "status": state["status"]})

    app = web.Application()
    app.router.add_post("/v3/payments", create)
    app.router.add_get("/v3/payments/{pid}", status)
    server = TestServer(app)
    await server.start_server()
    service = YooKassaService("123456", "test_secret", base_url=str(server.make_url("")).rstrip("/"))
    yield service, calls, state
    await service.close()
    await server.close()


async def test_create_payment_sends_receipt_with_email(fake_yookassa):
    service, calls, _ = fake_yookassa
    pid, url = await service.create_payment(19900, "5 песен", email="ivan@example.com", metadata={"payment": 7})
    assert (pid, url) == ("pay-1", "https://yoomoney.ru/checkout/pay-1")
    sent = calls[0]
    assert sent["headers"]["Authorization"] == "Basic " + base64.b64encode(b"123456:test_secret").decode()
    assert sent["headers"]["Idempotence-Key"]
    body = sent["body"]
    assert body["amount"] == {"value": "199.00", "currency": "RUB"} and body["capture"] is True
    assert body["metadata"] == {"payment": 7}
    receipt = body["receipt"]
    assert receipt["customer"] == {"email": "ivan@example.com"}
    assert receipt["items"][0]["amount"] == {"value": "199.00", "currency": "RUB"}


async def test_no_email_uses_shop_email_or_no_receipt(fake_yookassa):
    service, calls, _ = fake_yookassa
    await service.create_payment(19900, "5 песен", email=None)
    assert "receipt" not in calls[-1]["body"]
    service.receipt_email = "shop@example.com"
    await service.create_payment(19900, "5 песен", email=None)
    assert calls[-1]["body"]["receipt"]["customer"] == {"email": "shop@example.com"}


async def test_error_keeps_exact_yookassa_answer(fake_yookassa):
    service, _, state = fake_yookassa
    state["fail"] = {"type": "error", "id": "019a-err", "code": "invalid_credentials",
                     "description": "Error in shopId or secret key"}
    with pytest.raises(YooKassaError) as err:
        await service.create_payment(19900, "5 песен", email=None)
    assert err.value.status == 401
    assert json.loads(err.value.body)["id"] == "019a-err" and "019a-err" in str(err.value)


async def test_get_status(fake_yookassa):
    service, _, state = fake_yookassa
    assert await service.get_status("pay-1") == "pending"
    state["status"] = "succeeded"
    assert await service.get_status("pay-1") == "succeeded"


def test_not_configured():
    assert not YooKassaService(None, None).configured
    assert YooKassaService("1", "k").configured

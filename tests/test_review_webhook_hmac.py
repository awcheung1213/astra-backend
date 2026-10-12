"""Pure review-webhook HMAC tests (no DB, no client fixture): X-Evergreen-Signature
must be hex HMAC-SHA256(WEBHOOK_SECRET, raw body), compared in constant time,
before any parse or write. Brand/Review/create_task are faked — a rejected
delivery must never reach them."""

import hashlib
import hmac
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.webhooks as webhooks
import middleware.logging_middleware as logging_middleware

SECRET = "unit-test-webhook-secret"
BODY = json.dumps({"brand_id": "brand-1", "platform": "google", "external_id": "rev-1", "rating": 2, "author": "A", "text": "slow service"}).encode()


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def calls(monkeypatch):
    calls: list[str] = []

    async def get_brand(brand_id):
        calls.append(f"brand.get:{brand_id}")
        return types.SimpleNamespace(id=brand_id)

    async def get_by_external_id(brand_id, platform, external_id):
        calls.append("review.get_by_external_id")
        return None

    async def create_review(**fields):
        calls.append(f"review.create:{fields['external_id']}")
        return types.SimpleNamespace(id="review-1")

    async def create_task(**fields):
        calls.append(f"create_task:{fields['name']}")

    monkeypatch.setattr(webhooks.settings, "WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhooks, "Brand", types.SimpleNamespace(get=get_brand))
    monkeypatch.setattr(webhooks, "Review", types.SimpleNamespace(get_by_external_id=get_by_external_id, create=create_review))
    monkeypatch.setattr(webhooks, "create_task", create_task)
    return calls


def client(with_logging: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(webhooks.router)
    if with_logging:
        app.add_middleware(logging_middleware.LoggingMiddleware)
    return TestClient(app)


def post(body: bytes, signature: str | None, with_logging: bool = False):
    headers = {"content-type": "application/json"}
    if signature is not None:
        headers["X-Evergreen-Signature"] = signature
    return client(with_logging).post("/api/v1/webhooks/reviews", content=body, headers=headers)


@pytest.mark.parametrize("signature", [sign(BODY), f"sha256={sign(BODY)}", sign(BODY).upper()])
def test_valid_signature_is_accepted(calls, signature):
    response = post(BODY, signature)
    assert response.status_code == 200
    assert response.json() == {"review_id": "review-1", "queued_work": ["review_response"]}
    assert calls == ["brand.get:brand-1", "review.get_by_external_id", "review.create:rev-1", "create_task:review_response"]


@pytest.mark.parametrize(
    "body, signature",
    [
        (BODY, None),  # unsigned
        (BODY, ""),  # empty header
        (BODY, "not-hex"),  # malformed
        (BODY, "sha1=" + sign(BODY)),  # wrong scheme
        (BODY, sign(BODY)[:-2]),  # truncated
        (BODY, sign(BODY, "wrong-secret")),  # forged with another key
        (BODY.replace(b'"rating": 2', b'"rating": 5'), sign(BODY)),  # tampered body, original signature
        (b"{not json", sign(BODY)),  # garbage body is rejected by the signature, not parsed
    ],
)
def test_bad_signature_is_401_with_no_db_access(calls, body, signature):
    response = post(body, signature)
    assert response.status_code == 401
    assert calls == []


def test_unset_secret_rejects_everything(calls, monkeypatch):
    monkeypatch.setattr(webhooks.settings, "WEBHOOK_SECRET", "")
    assert post(BODY, sign(BODY, "")).status_code == 401
    assert post(BODY, sign(BODY)).status_code == 401
    assert calls == []


def test_signed_invalid_payload_is_422_after_verification(calls):
    body = json.dumps({"brand_id": "brand-1"}).encode()
    assert post(body, sign(body)).status_code == 422
    assert calls == []


def test_compare_is_constant_time(calls, monkeypatch):
    compared: list[tuple] = []
    real = hmac.compare_digest

    def spy(a, b):
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(webhooks.hmac, "compare_digest", spy)
    assert post(BODY, sign(BODY, "wrong-secret")).status_code == 401
    assert compared == [(sign(BODY), sign(BODY, "wrong-secret"))]


def test_signature_header_is_redacted_and_body_survives_logging_middleware(calls, monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(logging_middleware.logger, "debug", lambda message, *args, **kwargs: logged.append(str(message)))
    signature = sign(BODY)
    assert post(BODY, signature, with_logging=True).status_code == 200
    assert signature not in "\n".join(logged)

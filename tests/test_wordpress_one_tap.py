"""Pure (no DB, no client fixture): WordPress one-tap connect via WordPress's
own authorize-application screen, plus the readable manual-connect error.

URL building, the signed state (shared with Google's callback, provider
"wordpress"), and the public callback's outcomes — the verify-then-store path
it shares with POST /site is monkeypatched, so no network and no rows."""

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import api.connections as connections
import settings
from adapters.cms.common import CmsWriteError
from api.connections import WORDPRESS_APP_ID, WORDPRESS_REJECTED_DETAIL, mint_state, normalize_wordpress_site_url, verify_failure_detail, wordpress_authorize_url, wordpress_return_url, wordpress_state_brand
from middleware.logging_middleware import RedactAccessLogFilter, redact_url

CALLBACK = "https://api.evergreen.example/api/v1/connections/wordpress/callback"


##############################
# URL building
##############################


@pytest.mark.parametrize(
    ("typed", "fallback", "expected"),
    [
        ("reyesplumbing.com", "", "https://reyesplumbing.com"),
        ("  https://ReyesPlumbing.com/  ", "", "https://reyesplumbing.com"),
        ("http://reyesplumbing.com", "", "http://reyesplumbing.com"),
        ("https://reyesplumbing.com/wp-admin/", "", "https://reyesplumbing.com"),
        ("https://reyesplumbing.com/blog/wp-login.php?redirect_to=x", "", "https://reyesplumbing.com/blog"),
        ("https://reyesplumbing.com/blog/#top", "", "https://reyesplumbing.com/blog"),
        ("", "https://www.reyesplumbing.com/", "https://www.reyesplumbing.com"),
        ("   ", "reyesplumbing.com", "https://reyesplumbing.com"),
        ("", "", ""),
    ],
)
def test_normalize_site_url(typed, fallback, expected):
    assert normalize_wordpress_site_url(typed, fallback) == expected


def test_authorize_url_targets_wordpress_core_screen_with_signed_callbacks():
    url = wordpress_authorize_url("https://reyesplumbing.com", "STATE.sig", CALLBACK)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://reyesplumbing.com/wp-admin/authorize-application.php"
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert query["app_name"] == "Evergreen"
    assert query["app_id"] == WORDPRESS_APP_ID
    assert query["success_url"] == query["reject_url"] == f"{CALLBACK}?state=STATE.sig"


def test_app_id_is_a_stable_uuid():
    assert len(WORDPRESS_APP_ID) == 36 and WORDPRESS_APP_ID.count("-") == 4


def test_return_url_mirrors_google_back(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://app.evergreen.example")
    assert wordpress_return_url("connected") == "https://app.evergreen.example/?screen=connect&wordpress=1&result=connected"


def test_callback_url_defaults_beside_the_google_callback():
    assert settings.WORDPRESS_CONNECT_CALLBACK_URL.endswith("/api/v1/connections/wordpress/callback")


##############################
# state
##############################


def test_valid_wordpress_state_names_the_brand():
    assert wordpress_state_brand(mint_state("b1", "wordpress")) == ("ok", "b1")


def test_expired_state_is_told_apart_from_a_forged_one(monkeypatch):
    real_time = time.time
    state = mint_state("b1", "wordpress")
    monkeypatch.setattr(connections.time, "time", lambda: real_time() + connections.STATE_TTL_SECONDS + 60)
    assert wordpress_state_brand(state) == ("expired", "b1")


def test_tampered_garbage_and_google_states_are_invalid():
    payload, signature = mint_state("b1", "wordpress").rsplit(".", 1)
    forged_payload = base64.urlsafe_b64encode(json.dumps({**json.loads(base64.urlsafe_b64decode(payload)), "brand_id": "someone-else"}).encode()).decode()
    assert wordpress_state_brand(f"{forged_payload}.{signature}") == ("invalid", None)
    assert wordpress_state_brand("") == ("invalid", None)
    assert wordpress_state_brand("not-a-state") == ("invalid", None)
    assert wordpress_state_brand(mint_state("b1", "gsc")) == ("invalid", None)  # a Google state can't connect a site


##############################
# callback outcomes
##############################


@pytest.fixture
def connect_calls(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://app.evergreen.example")
    calls: dict = {"connected": [], "error": None}

    async def fake_connect(brand_id, platform, credentials):
        if calls["error"] is not None:
            raise calls["error"]
        calls["connected"].append((brand_id, platform, credentials))

    monkeypatch.setattr(connections, "connect_verified_site", fake_connect)
    return calls


def _result(response) -> str:
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://app.evergreen.example/?screen=connect&wordpress=1&result=")
    return parse_qs(urlsplit(location).query)["result"][0]


async def test_approved_callback_stores_through_the_shared_connect_path(connect_calls):
    response = await connections.wordpress_callback(state=mint_state("b1", "wordpress"), site_url="https://reyesplumbing.com/", user_login="marco", password="abcd EFGH ijkl MNOP qrst UVWX")
    assert _result(response) == "connected"
    assert connect_calls["connected"] == [("b1", "wordpress", {"base_url": "https://reyesplumbing.com", "username": "marco", "app_password": "abcd EFGH ijkl MNOP qrst UVWX"})]


async def test_rejected_or_incomplete_callback_stores_nothing(connect_calls):
    state = mint_state("b1", "wordpress")
    assert _result(await connections.wordpress_callback(state=state, success="false")) == "rejected"
    assert _result(await connections.wordpress_callback(state=state, site_url="https://reyesplumbing.com", user_login="marco")) == "rejected"
    assert _result(await connections.wordpress_callback(state=state)) == "rejected"
    assert connect_calls["connected"] == []


async def test_verify_failure_is_failed(connect_calls):
    connect_calls["error"] = CmsWriteError("wordpress GET /wp-json/wp/v2/pages -> 401: nope", status_code=401)
    response = await connections.wordpress_callback(state=mint_state("b1", "wordpress"), site_url="https://reyesplumbing.com", user_login="marco", password="pw")
    assert _result(response) == "failed"


async def test_expired_and_invalid_states_never_connect(monkeypatch, connect_calls):
    real_time = time.time
    stale = mint_state("b1", "wordpress")
    monkeypatch.setattr(connections.time, "time", lambda: real_time() + connections.STATE_TTL_SECONDS + 60)
    assert _result(await connections.wordpress_callback(state=stale, site_url="https://x.com", user_login="m", password="pw")) == "expired"
    monkeypatch.setattr(connections.time, "time", real_time)
    assert _result(await connections.wordpress_callback(state="forged.sig", site_url="https://x.com", user_login="m", password="pw")) == "failed"
    assert connect_calls["connected"] == []


async def test_password_never_reaches_the_log(monkeypatch, connect_calls):
    logged: list[str] = []
    monkeypatch.setattr(connections.logger, "info", lambda message, *args, **kwargs: logged.append(str(message)))
    monkeypatch.setattr(connections.logger, "error", lambda message, *args, **kwargs: logged.append(str(message)))
    await connections.wordpress_callback(state=mint_state("b1", "wordpress"), site_url="https://reyesplumbing.com", user_login="marco", password="SECRET-APP-PW")
    connect_calls["error"] = CmsWriteError("boom")
    await connections.wordpress_callback(state=mint_state("b1", "wordpress"), site_url="https://reyesplumbing.com", user_login="marco", password="SECRET-APP-PW")
    assert logged and not any("SECRET-APP-PW" in line for line in logged)


def test_request_and_access_logs_redact_the_query_password():
    url = "https://api.example/api/v1/connections/wordpress/callback?state=abc.def&site_url=https%3A%2F%2Fx.com&user_login=marco&password=SECRET+PW"
    redacted = redact_url(url)
    assert "SECRET" not in redacted and "password=*****" in redacted and "user_login=marco" in redacted and "state=abc.def" in redacted
    assert redact_url("https://api.example/health") == "https://api.example/health"

    import logging

    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:5", "GET", "/api/v1/connections/wordpress/callback?password=SECRET&state=s", "1.1", 302), None)
    assert RedactAccessLogFilter().filter(record)
    assert "SECRET" not in record.getMessage()


##############################
# manual connect's readable error
##############################


def test_wordpress_401_403_read_as_an_application_password_hint():
    for code in (401, 403):
        assert verify_failure_detail("wordpress", CmsWriteError("raw", status_code=code)) == WORDPRESS_REJECTED_DETAIL
    assert WORDPRESS_REJECTED_DETAIL == "WordPress didn't accept that. Use an application password, not your login password."


def test_other_failures_keep_the_raw_detail():
    assert verify_failure_detail("wordpress", CmsWriteError("wordpress GET x failed: timeout")) == "could not verify the connection: wordpress GET x failed: timeout"
    assert verify_failure_detail("wordpress", CmsWriteError("-> 500: oops", status_code=500)).startswith("could not verify the connection")
    assert verify_failure_detail("shopify", CmsWriteError("denied", status_code=401)).startswith("could not verify the connection")

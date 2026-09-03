"""Test authentication against the real Supabase project (geoviz pattern).

The suite signs in to the configured environment's Supabase project (GoTrue
password grant) with ONE dedicated login-only test-runner account and uses
its real access token; sign-in happens once and is cached for the session.
When the runner credentials aren't configured, auth-dependent tests SKIP
rather than fail (SUPABASE_URL may be the local sentinel).

One-org-per-user meets a suite that signs up constantly: release_runner()
frees the runner's users row (organization_id -> NULL) so the next signup
claims a fresh org. Call it before every signup — without it the second
signup in a session hands back the first unlaunched brand (the wizard
re-entry path) instead of creating anything."""

import httpx
import jwt
import pytest
from sqlalchemy import update

import settings
from objects.user import User
from utils.database import Database, ReplicaDatabase

# cached {"token": <jwt>, "sub": <uuid>, "email": <str>}, filled on first use
_RUNNER: dict[str, str] | None = None


def _config() -> dict | None:
    url = str(settings.SUPABASE_URL).strip()
    anon_key = str(getattr(settings, "SUPABASE_PUBLISHABLE_KEY", "")).strip()
    email = str(getattr(settings, "TEST_RUNNER_EMAIL", "")).strip()
    password = str(getattr(settings, "TEST_RUNNER_PASSWORD", ""))
    if not url or url == "https://test.invalid" or not anon_key or not email or not password:
        return None
    return {"url": url.rstrip("/"), "anon_key": anon_key, "email": email, "password": password}


def is_configured() -> bool:
    return _config() is not None


def _sign_in() -> dict[str, str]:
    global _RUNNER
    config = _config()
    if config is None:
        pytest.skip(f"Supabase test credentials not configured for the '{settings.ENVIRONMENT}' environment (SUPABASE_PUBLISHABLE_KEY + TEST_RUNNER_EMAIL/PASSWORD)")
    if _RUNNER is not None:
        return _RUNNER
    response = httpx.post(
        f"{config['url']}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": config["anon_key"], "Content-Type": "application/json"},
        json={"email": config["email"], "password": config["password"]},
        timeout=15.0,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Supabase sign-in failed for the test runner ({config['email']}): {response.status_code} {response.text}")
    token = response.json()["access_token"]
    # the token's `sub` is the Supabase auth.users uuid — decoded WITHOUT
    # verification here only to learn the id (the app still verifies for real)
    claims = jwt.decode(token, options={"verify_signature": False})
    _RUNNER = {"token": token, "sub": claims["sub"], "email": config["email"].strip().lower()}
    return _RUNNER


def runner_token() -> str:
    return _sign_in()["token"]


def runner_sub() -> str:
    return _sign_in()["sub"]


def runner_headers() -> dict:
    """Bearer header for the test runner. Skips the calling test when the
    Supabase test credentials aren't configured for this environment."""
    return {"Authorization": f"Bearer {runner_token()}"}


async def release_runner() -> None:
    """Free the runner from its org so the next signup claims a fresh one.
    The orphaned test org/brand stays for the post-suite scrub."""
    async with ReplicaDatabase().connection(), Database().connection():
        await Database.session.execute(update(User).where(User.supabase_user_id == runner_sub()).values(organization_id=None))  # type: ignore
        await Database.session.commit()

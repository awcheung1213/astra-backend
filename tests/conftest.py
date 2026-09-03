"""Shared test fixtures. Tests create their own rows and assume NOTHING about
DB seed state. Tests run inside docker compose (make test / make check) where
the database container is reachable.

The client carries the test runner's REAL Supabase bearer token (tests/_auth
signs in once per session against the staging project); API tests skip when
those credentials aren't configured. Worker-side tests build rows directly
and don't need it."""

import httpx
import pytest

from main import app
from tests import _auth


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=_auth.runner_headers()) as async_client:
        yield async_client


@pytest.fixture
async def anonymous_client():
    """No bearer token — for proving the guard actually guards."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as async_client:
        yield async_client

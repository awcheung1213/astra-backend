"""Shared test fixtures. Tests create their own rows and assume NOTHING about
DB seed state. Tests run inside docker compose (make test / make check) where
the database container is reachable."""

import httpx
import pytest

from main import app


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as async_client:
        yield async_client

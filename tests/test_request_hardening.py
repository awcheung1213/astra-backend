"""Pure request-path guards (no DB, no client fixture): a 500 rolls the request
transaction back, request-body secrets never reach the log, and the interview
extraction is typed before it touches the brand row."""

import contextlib
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import main
import middleware.database_session_middleware as database_session_middleware
import middleware.logging_middleware as logging_middleware
from api.onboarding import _parse_extraction
from middleware.database_session_middleware import DatabaseSessionMiddleware
from middleware.internal_server_error_middleware import InternalServerErrorMiddleware


def test_internal_server_error_wraps_database_session_in_main():
    # user_middleware[0] is outermost
    order = [entry.cls for entry in main.app.user_middleware]
    assert order.index(InternalServerErrorMiddleware) < order.index(DatabaseSessionMiddleware)


def test_unhandled_exception_rolls_back(monkeypatch):
    events: list[str] = []

    class FakeSession:
        @contextlib.asynccontextmanager
        async def transaction(self):
            try:
                yield
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

    class FakeDatabase:
        session = FakeSession()

        @contextlib.asynccontextmanager
        async def connection(self):
            yield

    monkeypatch.setattr(database_session_middleware, "Database", FakeDatabase)
    monkeypatch.setattr(database_session_middleware, "ReplicaDatabase", FakeDatabase)

    app = FastAPI()

    @app.post("/boom")
    async def boom():
        events.append("create_task flushed")
        raise RuntimeError("after create_task")

    for entry in reversed(main.app.user_middleware):  # re-add innermost first = main.py's order
        if entry.cls in (InternalServerErrorMiddleware, DatabaseSessionMiddleware):
            app.add_middleware(entry.cls)

    response = TestClient(app, raise_server_exceptions=False).post("/boom")
    assert response.status_code == 500
    assert events == ["create_task flushed", "rollback"]


def test_request_body_secrets_are_redacted(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(logging_middleware.logger, "debug", lambda message, *args, **kwargs: logged.append(str(message)))

    app = FastAPI()

    @app.post("/site")
    async def site():
        return {"ok": True}

    app.add_middleware(logging_middleware.LoggingMiddleware)
    body = {
        "platform": "wordpress",
        "credentials": {"base_url": "https://example.com", "username": "admin", "app_password": "wp-SECRET-1"},
        "client_secret": "shpss_SECRET-2",
        "nested": [{"refresh_token": "SECRET-3", "keep": "visible"}],
    }
    assert TestClient(app).post("/site", json=body).status_code == 200

    log = "\n".join(logged)
    assert "SECRET" not in log
    assert "wordpress" in log and "visible" in log


def _extraction(**overrides) -> str:
    return json.dumps({"description": "d", "category": "c", "audience": "a", "differentiators": ["x"], **overrides})


def test_extraction_lone_string_differentiator_becomes_a_list():
    assert _parse_extraction(_extraction(differentiators="family owned since 1990"))["differentiators"] == ["family owned since 1990"]


def test_extraction_null_fields_are_empty():
    assert _parse_extraction(_extraction(audience=None, differentiators=None)) == {"description": "d", "category": "c", "audience": "", "differentiators": []}


def test_extraction_blank_differentiators_are_dropped():
    assert _parse_extraction(_extraction(differentiators=[" a ", "", "  "]))["differentiators"] == ["a"]


@pytest.mark.parametrize("overrides", [{"differentiators": [1, 2]}, {"differentiators": {"a": 1}}, {"description": ["d"]}, {"category": 3}])
def test_extraction_wrong_types_are_rejected(overrides):
    with pytest.raises(HTTPException) as error:
        _parse_extraction(_extraction(**overrides))
    assert error.value.status_code == 502

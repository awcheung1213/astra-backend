"""Env readers for deployed settings: a missing OR empty required variable
(e.g. a Railway reference to a misnamed service resolves to "") fails boot
with the variable's name instead of a cryptic downstream error."""

import os


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable {name} is missing or empty (on Railway: check the service variables; a ${{{{Service.VAR}}}} reference to a misnamed service resolves to empty)")
    return value


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"environment variable {name} must be an integer, got {value!r}") from None

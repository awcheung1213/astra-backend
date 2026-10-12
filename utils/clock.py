"""Business-logic wall clock, with a TEST-ONLY simulated mode.

Every business-logic "what time is it" read goes through here (not
datetime.now()/date.today()/time.time()) so a verification run can fast-forward
the api AND the worker together. Infra time (JWT exp, OAuth state TTL, provider
tokens, monotonic timers, procrastinate internals) stays on the real clock.

Unset EVERGREEN_CLOCK_FILE → real time: each call is one module-global check,
then the exact stdlib call it replaced.

Set → the file holds {"anchor_real": <epoch>, "anchor_sim": <epoch>, "scale": <float>}
and sim_epoch = anchor_sim + (time.time() - anchor_real) * scale. The file is
re-read when its mtime changes (stat at most every 0.5s), so an external tool can
jump or rescale time while both processes run. Missing/invalid → real time, one
warning.
"""

import json
import os
import time as _time
from collections.abc import Callable
from datetime import date, datetime

from utils.logger import logger

CLOCK_FILE = os.environ.get("EVERGREEN_CLOCK_FILE") or None
STAT_INTERVAL_SECONDS = 0.5


class _ClockState:
    def __init__(self) -> None:
        # identity mapping (sim == real) until a valid file is read
        self.anchor_real = 0.0
        self.anchor_sim = 0.0
        self.scale = 1.0
        self.mtime: float | None = None
        self.checked_at: float | None = None
        self.warned = False


_state = _ClockState()


def _refresh(real: float) -> None:
    """Re-read the clock file if its mtime moved; throttled to one stat per interval."""
    if _state.checked_at is not None and real - _state.checked_at < STAT_INTERVAL_SECONDS:
        return
    _state.checked_at = real
    try:
        mtime = os.stat(CLOCK_FILE).st_mtime  # type: ignore[arg-type]
    except OSError:
        _state.mtime = None  # a re-created file is re-read whatever its mtime
        _fallback(f"clock file {CLOCK_FILE} missing")
        return
    if mtime == _state.mtime:
        return
    _state.mtime = mtime
    try:
        with open(CLOCK_FILE) as handle:  # type: ignore[arg-type]
            config = json.load(handle)
        anchor_real, anchor_sim, scale = float(config["anchor_real"]), float(config["anchor_sim"]), float(config["scale"])
        if scale <= 0:
            raise ValueError("scale must be > 0")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _fallback(f"clock file {CLOCK_FILE} invalid ({exc})")
        return
    _state.anchor_real, _state.anchor_sim, _state.scale = anchor_real, anchor_sim, scale
    _state.warned = False
    logger.info(f"[clock] simulated time: anchor_sim={datetime.fromtimestamp(anchor_sim).isoformat()} scale={scale}")


def _fallback(reason: str) -> None:
    _state.anchor_real, _state.anchor_sim, _state.scale = 0.0, 0.0, 1.0
    if not _state.warned:
        _state.warned = True
        logger.warning(f"[clock] {reason} — using real time")


def time() -> float:
    """Epoch seconds (simulated when the clock file is set)."""
    if CLOCK_FILE is None:
        return _time.time()
    real = _time.time()
    _refresh(real)
    return _state.anchor_sim + (real - _state.anchor_real) * _state.scale


def now() -> datetime:
    """Naive local datetime — same semantics as datetime.now() (containers run UTC)."""
    if CLOCK_FILE is None:
        return datetime.now()
    return datetime.fromtimestamp(time())


def today() -> date:
    if CLOCK_FILE is None:
        return date.today()
    return now().date()


def scale() -> float:
    """Sim seconds per real second (1.0 in real mode)."""
    if CLOCK_FILE is None:
        return 1.0
    _refresh(_time.time())
    return _state.scale


def column_default() -> Callable[[], datetime] | None:
    """Python-side default for server_default=func.now() columns: sim mode stamps
    ORM/Core inserts with simulated time; real mode returns None so Postgres
    stamps exactly as before (decided once at import, like CLOCK_FILE)."""
    return now if CLOCK_FILE is not None else None

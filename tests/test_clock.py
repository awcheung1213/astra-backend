"""utils/clock + the sim-time periodic deferrer — pure unit tests, no DB."""

import json
import os
from calendar import timegm
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from procrastinate import periodic

from utils import clock
from worker import run
from worker.run import SIM_MAX_DELAY_SECONDS, SimPeriodicDeferrer, real_sleep_seconds

REAL_NOW = 1_800_000_000.0
SIM_ANCHOR = 1_900_000_000.0


class FakeTime:
    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now


@pytest.fixture
def fake_time(monkeypatch):
    fake = FakeTime(REAL_NOW)
    monkeypatch.setattr(clock, "_time", fake)
    return fake


@pytest.fixture
def clock_file(tmp_path, monkeypatch):
    path = tmp_path / "clock.json"
    monkeypatch.setattr(clock, "CLOCK_FILE", str(path))
    monkeypatch.setattr(clock, "_state", clock._ClockState())
    return path


def _write(path, anchor_real: float, anchor_sim: float, scale: float, mtime: float) -> None:
    path.write_text(json.dumps({"anchor_real": anchor_real, "anchor_sim": anchor_sim, "scale": scale}))
    os.utime(path, (mtime, mtime))


##############################
# CLOCK
##############################


def test_real_mode_passthrough(monkeypatch):
    monkeypatch.setattr(clock, "CLOCK_FILE", None)
    before = datetime.now()
    assert before <= clock.now() <= datetime.now()
    assert clock.today() == date.today()
    assert abs(clock.time() - datetime.now().timestamp()) < 1
    assert clock.scale() == 1.0
    assert clock.column_default() is None  # Postgres keeps stamping created_at exactly as before


def test_file_offset_and_scale(fake_time, clock_file):
    _write(clock_file, anchor_real=REAL_NOW, anchor_sim=SIM_ANCHOR, scale=60.0, mtime=1.0)
    assert clock.time() == SIM_ANCHOR
    fake_time.now += 10  # 10 real seconds = 10 sim minutes
    assert clock.time() == SIM_ANCHOR + 600
    assert clock.now() == datetime.fromtimestamp(SIM_ANCHOR + 600)
    assert clock.today() == datetime.fromtimestamp(SIM_ANCHOR + 600).date()
    assert clock.scale() == 60.0
    assert clock.column_default() is clock.now


def test_mtime_change_reloads_throttled(fake_time, clock_file):
    _write(clock_file, anchor_real=REAL_NOW, anchor_sim=SIM_ANCHOR, scale=1.0, mtime=1.0)
    assert clock.time() == SIM_ANCHOR

    # an external tool jumps a day ahead: not seen inside the stat interval...
    _write(clock_file, anchor_real=REAL_NOW, anchor_sim=SIM_ANCHOR + 86400, scale=1.0, mtime=2.0)
    fake_time.now += 0.1
    assert clock.time() == pytest.approx(SIM_ANCHOR + 0.1)
    # ...seen on the next stat
    fake_time.now += clock.STAT_INTERVAL_SECONDS
    assert clock.time() == pytest.approx(SIM_ANCHOR + 86400 + 0.6)


def test_unchanged_mtime_is_not_reread(fake_time, clock_file):
    _write(clock_file, anchor_real=REAL_NOW, anchor_sim=SIM_ANCHOR, scale=1.0, mtime=1.0)
    assert clock.time() == SIM_ANCHOR
    _write(clock_file, anchor_real=REAL_NOW, anchor_sim=0.0, scale=1.0, mtime=1.0)
    fake_time.now += 1
    assert clock.time() == SIM_ANCHOR + 1


@pytest.mark.parametrize("content", ["not json", json.dumps({"anchor_real": 1.0}), json.dumps({"anchor_real": 1.0, "anchor_sim": 2.0, "scale": 0})])
def test_invalid_file_falls_back_to_real_time(fake_time, clock_file, content):
    clock_file.write_text(content)
    assert clock.time() == REAL_NOW
    assert clock.scale() == 1.0
    assert clock._state.warned


def test_missing_file_falls_back_then_picks_file_up(fake_time, clock_file):
    assert clock.time() == REAL_NOW
    _write(clock_file, anchor_real=REAL_NOW, anchor_sim=SIM_ANCHOR, scale=1.0, mtime=1.0)
    fake_time.now += clock.STAT_INTERVAL_SECONDS
    assert clock.time() == SIM_ANCHOR + clock.STAT_INTERVAL_SECONDS


##############################
# SIM PERIODIC DEFERRER
##############################


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return timegm(datetime(year, month, day, hour, minute).timetuple())


class RecordingDeferrer(SimPeriodicDeferrer):
    """Records what would be deferred / slept instead of touching the DB."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.deferred: list[tuple[str, int]] = []
        self.sleeps: list[float] = []

    async def defer_jobs(self, jobs_to_defer) -> None:
        self.deferred.extend((periodic_task.task.name, timestamp) for periodic_task, timestamp in jobs_to_defer)

    async def wait(self, next_tick: float) -> None:
        self.sleeps.append(real_sleep_seconds(next_tick, clock.scale()))


def _registry(**crons: str) -> periodic.PeriodicRegistry:
    registry = periodic.PeriodicRegistry()
    for name, cron in crons.items():
        registry.periodic_tasks[(name, "")] = periodic.PeriodicTask(task=SimpleNamespace(name=name), cron=cron, periodic_id="", configure_kwargs={})  # type: ignore[arg-type]
    return registry


@pytest.fixture
def sim_now(monkeypatch):
    state = SimpleNamespace(epoch=0.0, scale=3600.0)
    monkeypatch.setattr(clock, "time", lambda: state.epoch)
    monkeypatch.setattr(clock, "scale", lambda: state.scale)
    return state


async def test_deferrer_uses_sim_epochs_and_catches_up_across_a_jump(sim_now):
    deferrer = RecordingDeferrer(registry=_registry(daily="0 0 * * *", weekly="0 2 * * 1"), max_delay=SIM_MAX_DELAY_SECONDS)

    sim_now.epoch = _utc(2031, 3, 5, 12)  # Wednesday noon
    await deferrer.tick()
    # large max_delay seeds every schedule with its most recent past fire (sim epochs)
    assert deferrer.deferred == [("weekly", _utc(2031, 3, 3, 2)), ("daily", _utc(2031, 3, 5))]

    deferrer.deferred.clear()
    sim_now.epoch = _utc(2031, 3, 11, 3)  # jump to next Tuesday 03:00
    await deferrer.tick()
    # every missed fire, chronological across schedules
    assert deferrer.deferred == [
        ("daily", _utc(2031, 3, 6)),
        ("daily", _utc(2031, 3, 7)),
        ("daily", _utc(2031, 3, 8)),
        ("daily", _utc(2031, 3, 9)),
        ("daily", _utc(2031, 3, 10)),
        ("weekly", _utc(2031, 3, 10, 2)),
        ("daily", _utc(2031, 3, 11)),
    ]

    deferrer.deferred.clear()
    await deferrer.tick()  # same sim instant: nothing new
    assert deferrer.deferred == []


async def test_default_max_delay_would_drop_the_first_fire(sim_now):
    """Why sim mode raises max_delay: stock 10 minutes never seeds a key whose fire is hours old."""
    deferrer = RecordingDeferrer(registry=_registry(daily="0 0 * * *"))
    sim_now.epoch = _utc(2031, 3, 5, 12)
    await deferrer.tick()
    assert deferrer.deferred == []


async def test_deferrer_sleeps_real_seconds(sim_now):
    deferrer = RecordingDeferrer(registry=_registry(hourly="0 * * * *"), max_delay=SIM_MAX_DELAY_SECONDS)
    sim_now.epoch = _utc(2031, 3, 5, 12, 30)
    await deferrer.tick()
    assert deferrer.sleeps == [pytest.approx((1800 + periodic.MARGIN) / 3600)]


def test_real_sleep_seconds_clamps():
    assert real_sleep_seconds(1800, 3600) == pytest.approx(1800.5 / 3600)
    assert real_sleep_seconds(86400, 1) == run.SIM_MAX_SLEEP_SECONDS
    assert real_sleep_seconds(0, 1000) == run.SIM_MIN_SLEEP_SECONDS


def test_install_swaps_the_deferrer_procrastinate_builds(monkeypatch):
    from procrastinate import worker as procrastinate_worker

    monkeypatch.setattr(periodic, "PeriodicDeferrer", periodic.PeriodicDeferrer)  # restored after the test
    monkeypatch.setattr(run.app, "periodic_defaults", {})
    monkeypatch.setattr(clock, "CLOCK_FILE", "/nonexistent/clock.json")
    monkeypatch.setattr(clock, "_state", clock._ClockState())
    run._install_sim_deferrer()
    assert procrastinate_worker.periodic.PeriodicDeferrer is SimPeriodicDeferrer  # Worker._periodic_deferrer's lookup
    assert run.app.periodic_defaults["max_delay"] == SIM_MAX_DELAY_SECONDS

"""Worker service entrypoint.

Railway start command:  python -m worker.run   (custom start command on the worker service)
Local:                  the evergreen-backend-worker service in docker compose
"""

import asyncio

from procrastinate import periodic

import bootstrap  # noqa
import settings
from utils import clock
from utils.logger import logger
from worker.app import app

SIM_MAX_DELAY_SECONDS = 10 * 365 * 86400  # sim mode: never drop an overdue fire (see SimPeriodicDeferrer)
SIM_MIN_SLEEP_SECONDS = 0.2
SIM_MAX_SLEEP_SECONDS = 5.0  # wake often enough to notice an external clock jump


class SimPeriodicDeferrer(periodic.PeriodicDeferrer):
    """Procrastinate's periodic deferrer on SIMULATED time (test-only, utils/clock).

    Hook: procrastinate 3.9's Worker._periodic_deferrer() builds its deferrer as
    `periodic.PeriodicDeferrer(registry=..., **app.periodic_defaults)` — a module
    attribute lookup at call time — so _main() swaps that attribute for this
    subclass (sim mode only, worker process only). Chosen over patching the
    module's `time` import (fixes "now" but leaves the sleep in sim seconds) and
    over subclassing Worker/App (overriding private _periodic_deferrer/_worker
    and re-plumbing run_worker_async).

    Only "now" and the sleep change. Cron math, the last_defers catch-up (every
    missed fire, in order, after a forward jump) and procrastinate_periodic_defers
    dedup are the library's own code fed sim epochs — so periodic `timestamp`
    args and dedup keys are sim epochs. max_delay only gates keys never deferred
    yet (startup); sim mode raises it so a key that wasn't due in the first
    minutes still gets seeded — otherwise a later jump would skip it entirely.
    Side effect: the first tick defers each schedule's most recent past fire
    (unless already recorded in procrastinate_periodic_defers).
    """

    async def worker(self) -> None:
        if not self.registry.periodic_tasks:
            return await super().worker()  # logs "no periodic task" and returns
        while True:
            await self.tick()

    async def tick(self) -> None:
        now = clock.time()
        await self.defer_jobs(jobs_to_defer=self.get_previous_tasks(at=now))
        await self.wait(next_tick=self.get_next_tick(at=now))

    def get_previous_tasks(self, at: float) -> list[periodic.TaskAtTime]:
        # a jump can span many fires of many schedules: defer them chronologically across schedules, not schedule by schedule
        return sorted(super().get_previous_tasks(at=at), key=lambda task_at_time: task_at_time[1])

    async def wait(self, next_tick: float) -> None:
        await asyncio.sleep(real_sleep_seconds(next_tick, clock.scale()))


def real_sleep_seconds(sim_seconds: float, scale: float) -> float:
    """Sim seconds until the next fire → real seconds to sleep, clamped."""
    return min(max((sim_seconds + periodic.MARGIN) / scale, SIM_MIN_SLEEP_SECONDS), SIM_MAX_SLEEP_SECONDS)


def _install_sim_deferrer() -> None:
    periodic.PeriodicDeferrer = SimPeriodicDeferrer  # type: ignore[misc]
    app.periodic_defaults["max_delay"] = SIM_MAX_DELAY_SECONDS
    logger.info(f"boot: simulated clock ({clock.CLOCK_FILE}) — periodic deferrer on sim time, now={clock.now().isoformat()} scale={clock.scale()}")


async def _main() -> None:
    logger.info(f"boot: service=worker env={settings.ENVIRONMENT} concurrency={settings.WORKER_CONCURRENCY}")
    if clock.CLOCK_FILE is not None:
        _install_sim_deferrer()
    async with app.open_async():
        await app.run_worker_async(concurrency=settings.WORKER_CONCURRENCY)


if __name__ == "__main__":
    asyncio.run(_main())

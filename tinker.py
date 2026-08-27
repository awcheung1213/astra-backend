"""Scratch driver — run one-off steps against the dev DB.

    docker compose run --rm --no-deps astra-backend python tinker.py

Step 1 seeds (markets + test brand world); seed.py is re-run safe, so
running tinker repeatedly won't duplicate anything. The one-off AM task
runs (weekly_brief / client_chase) get added here next — they'll need the
seeded brand_id printed by the seed step."""

import asyncio

from seed import seed


async def tinker() -> None:
    # ---- step 1: seed the dev database --------------------------------
    await seed()

    # ---- step 2 (next): one-off AM runs -------------------------------
    # create_task(name="weekly_brief"/"client_chase") for the seeded brand,
    # then invoke the runners directly — no worker needed. Spends real
    # Anthropic tokens; added once the seed output is approved.


if __name__ == "__main__":
    asyncio.run(tinker())

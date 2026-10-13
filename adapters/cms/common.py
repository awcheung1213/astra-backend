"""Shared pieces of the CMS write adapters (the change-set engine's arms)."""

from typing import Awaitable, Callable
from urllib.parse import urlparse

import markdown as markdown_lib

WRITE_TIMEOUT_SECONDS = 20.0


class CmsWriteError(Exception):
    """A CMS write failed. Propagates out of change_set_apply so procrastinate
    retries (the completed-op markers make the retry resume); exhausted
    retries dead-letter the task to the operator. status_code: the CMS's
    HTTP status when it answered (None for transport errors) — lets connect
    tell "wrong password" apart from "site down"."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


Checkpoint = Callable[[dict], Awaitable[None]]
"""How an adapter hands the engine progress mid-operation: {"previous_value"}
BEFORE its write, {"created_ref"} right after a create. The engine COMMITS it
on the op before the adapter goes on, so a crash after the site write can't
lose the rollback record — and a retry keeps the first captured value
(objects/change_set.merge_operation_progress), never the re-read NEW one."""


async def no_checkpoint(fields: dict) -> None:
    return None


def path_slug(url: str) -> str:
    """The page's slug/handle: the last non-empty path segment of its URL."""
    segments = [segment for segment in urlparse(url).path.split("/") if segment]
    if not segments:
        raise CmsWriteError(f"cannot derive a page slug from {url!r}")
    return segments[-1]


def markdown_to_html(body_markdown: str) -> str:
    return markdown_lib.markdown(body_markdown)

"""Shared pieces of the CMS write adapters (the change-set engine's arms)."""

from urllib.parse import urlparse

import markdown as markdown_lib

WRITE_TIMEOUT_SECONDS = 20.0


class CmsWriteError(Exception):
    """A CMS write failed. Propagates out of change_set_apply so procrastinate
    retries (the completed-op markers make the retry resume); exhausted
    retries dead-letter the task to the operator."""


def path_slug(url: str) -> str:
    """The page's slug/handle: the last non-empty path segment of its URL."""
    segments = [segment for segment in urlparse(url).path.split("/") if segment]
    if not segments:
        raise CmsWriteError(f"cannot derive a page slug from {url!r}")
    return segments[-1]


def markdown_to_html(body_markdown: str) -> str:
    return markdown_lib.markdown(body_markdown)

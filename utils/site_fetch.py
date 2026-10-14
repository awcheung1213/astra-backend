"""Reading a CLIENT's website — every request to a customer's own site goes
through here (audit crawl, fix generators, judgment page reads, the uptime
check, the WordPress REST adapter).

curl_cffi instead of httpx, on purpose: hosts' bot protection (Hostinger's
hCDN, Cloudflare, ...) fingerprints the TLS/HTTP handshake and 403s plain
Python clients no matter the User-Agent — the crawl, the fix generator and the
uptime check all read a "blank" or "down" site while a browser sees it fine.
Presenting a real browser's handshake is what gets through with zero
configuration on the client's site. Safari and Firefox profiles pass
Hostinger's challenge; every Chrome/Edge profile fails it (verified
2026-09-14) — don't "upgrade" IMPERSONATE_BROWSER to Chrome.

Pure fingerprint blocks are what this beats. A JavaScript challenge (Cloudflare
"under attack" mode) still blocks it; that needs a headless browser, deferred
until a client site actually needs it."""

import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession, RequestsError, Response

__all__ = ["AsyncSession", "RequestsError", "Response", "fetch", "fetch_page", "fetch_page_html", "site_session"]

IMPERSONATE_BROWSER = "safari"
DEFAULT_TIMEOUT_SECONDS = 10.0


def site_session(timeout: float = DEFAULT_TIMEOUT_SECONDS, **session_kwargs) -> AsyncSession:
    """A browser-fingerprinted session; use as `async with site_session() as client`.
    Extra kwargs pass through (e.g. auth=(user, password) for authed REST reads)."""
    return AsyncSession(impersonate=IMPERSONATE_BROWSER, allow_redirects=True, timeout=timeout, **session_kwargs)


# Hosts put a page cache in front of WordPress (Hostinger's hCDN + LiteSpeed
# served evergreenofficial.com 27h stale on 2026-10-13): a plain GET sees the
# site as it was, not as it is — a plugin the owner just installed, or a fix we
# just applied, stays invisible until the cache expires. A unique query string
# + no-cache headers make the cache pass the request through.
FRESH_PARAM = "evergreen_fresh"
NO_CACHE_HEADERS = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


def fresh_url(url: str, stamp: int) -> str:
    """The URL with a cache-busting param added (existing query kept). Pure."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != FRESH_PARAM] + [(FRESH_PARAM, str(stamp))]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def fetch(client: AsyncSession, url: str, fresh: bool = False) -> Response | None:
    """GET on an open session; None on any transport error (DNS, TLS, timeout).
    fresh=True bypasses page caches (see fresh_url) — the audit reads the live site."""
    try:
        if fresh:
            return await client.get(fresh_url(url, time.time_ns()), headers=NO_CACHE_HEADERS)
        return await client.get(url)
    except RequestsError:
        return None


async def fetch_page(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Response | None:
    """One-off GET with its own session; None on any transport error."""
    async with site_session(timeout=timeout) as client:
        return await fetch(client, url)


async def fetch_page_html(url: str) -> str | None:
    """The page body, or None when the fetch failed or the server answered 4xx/5xx."""
    response = await fetch_page(url)
    if response is None or response.status_code >= 400:
        return None
    return response.text

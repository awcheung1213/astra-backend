"""MX lookup over DNS-over-HTTPS (Google's JSON resolver) — how the outreach
mailer guesses who hosts an owner's custom-domain mailbox (Google Workspace,
Microsoft 365, or someone else) before he has connected anything.

A free public read: no cost event (the google_adapter precedent — cost events
are written at spend sites; a $0 read has none). Cached per domain for the
life of the process, failures included: a mailbox host does not move between
requests, and a resolver hiccup must not turn every page load into a retry.
Any failure answers None ("unknown"), never raises — route selection then
falls back to the domain-agnostic route."""

import httpx

from utils.logger import logger

DOH_URL = "https://dns.google/resolve"
DOH_TIMEOUT_SECONDS = 4.0
MX_RECORD_TYPE = 15

_mx_cache: dict[str, list[str] | None] = {}


def parse_mx_answer(payload: dict) -> list[str]:
    """MX exchange hosts from a DoH JSON answer, lowest preference first,
    lower-cased, trailing dot dropped. Pure."""
    records = []
    for answer in payload.get("Answer") or []:
        if answer.get("type") != MX_RECORD_TYPE:
            continue
        parts = str(answer.get("data") or "").split()
        if len(parts) != 2:
            continue
        try:
            preference = int(parts[0])
        except ValueError:
            continue
        records.append((preference, parts[1].rstrip(".").lower()))
    return [host for _, host in sorted(records) if host]


async def mx_hosts(domain: str) -> list[str] | None:
    """The domain's MX hosts, or None when the lookup failed (unknown)."""
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return None
    if domain in _mx_cache:
        return _mx_cache[domain]
    hosts: list[str] | None
    try:
        async with httpx.AsyncClient(timeout=DOH_TIMEOUT_SECONDS) as client:
            response = await client.get(DOH_URL, params={"name": domain, "type": "MX"}, headers={"Accept": "application/dns-json"})
        hosts = parse_mx_answer(response.json()) if response.status_code == 200 else None
    except Exception as error:
        logger.info(f"[dns] MX lookup for {domain} failed: {type(error).__name__}")
        hosts = None
    _mx_cache[domain] = hosts
    return hosts

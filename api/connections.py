"""Site connect — how measuring becomes fixing: credentials are verified by
a LIVE read through the same adapter that will later write with them, then
encrypted and stored. Nothing is stored on a failed verify. A successful
connect closes the grant_access client task (the chase stops nagging) and
fires the site audit README promises "at site connect".

⚠ NO AUTH YET (repo-wide) — these routes accept secrets, so the auth port
must land before this deploys anywhere public. Local dev only until then."""

import re

from fastapi import APIRouter, HTTPException, status

from adapters.cms.common import CmsWriteError
from objects.brand import PLATFORM_SHOPIFY, PLATFORM_WORDPRESS, Brand
from objects.connection import Connection
from objects.site import STATUS_CONNECTED, STATUS_DISCONNECTED, STATUS_ERROR, Site
from objects.task import STATUS_DONE, Task
from schemas.connection_schemas import ConnectionsResponse, ProviderStatusResponse, SiteConnectRequest, SiteStatusResponse
from utils.task_queue import create_task
from utils.token_crypto import encrypt_credentials
from worker.tasks.changes import ADAPTER_BY_PLATFORM

router = APIRouter(prefix="/api/v1")

REQUIRED_CREDENTIAL_KEYS = {
    PLATFORM_WORDPRESS: ("base_url", "username", "app_password"),
    PLATFORM_SHOPIFY: ("shop_domain", "access_token"),
}


@router.get("/brands/{brand_id}/connections", response_model=ConnectionsResponse)
async def connections(brand_id: str) -> ConnectionsResponse:
    brand = await Brand.get(brand_id)
    site = await Site.any_for_brand(brand.id)
    return ConnectionsResponse.create(
        {
            "site": _site_status(site) if site else None,
            "providers": [ProviderStatusResponse.create({"provider": connection.provider, "status": connection.status, "connected_at": connection.connected_at.isoformat() if connection.connected_at else None}) for connection in await Connection.for_brand(brand.id)],
        }
    )


@router.post("/brands/{brand_id}/site", response_model=SiteStatusResponse)
async def connect_site(brand_id: str, request: SiteConnectRequest) -> SiteStatusResponse:
    brand = await Brand.get(brand_id)
    if request.platform not in REQUIRED_CREDENTIAL_KEYS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"platform must be one of {sorted(REQUIRED_CREDENTIAL_KEYS)}")
    credentials = _normalized_credentials(request.platform, request.credentials)
    encrypted = encrypt_credentials(credentials)

    # verify with the SAME adapter that will later write through these
    # credentials — an unsaved probe row, so nothing persists on failure
    probe = Site(brand_id=brand.id, platform=request.platform, credentials_encrypted=encrypted)
    try:
        await ADAPTER_BY_PLATFORM[request.platform](probe).verify()  # type: ignore[attr-defined]
    except CmsWriteError as error:
        # a failed RE-connect must not clobber a working connection
        existing = await Site.any_for_brand(brand.id)
        if existing is None or existing.status != STATUS_CONNECTED:
            await Site.upsert_for_brand(brand.id, request.platform, STATUS_ERROR, credentials_encrypted=None)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"could not verify the connection: {error}")

    site = await Site.upsert_for_brand(brand.id, request.platform, STATUS_CONNECTED, credentials_encrypted=encrypted)
    await Brand.update(brand.id, platform=request.platform)

    # the connection IS what grant_access asked for — stop the daily chase
    grant_access_task = await Task.first_queued(brand.id, "grant_access")
    if grant_access_task is not None:
        await Task.update_status(grant_access_task.id, STATUS_DONE)

    # README: the audit "fires at site connect, then weekly" — this is the
    # connect half (reconnects re-audit on purpose; the crawl is cheap)
    await create_task(brand_id=brand.id, name="site_audit", queue="audit")
    return _site_status(site)


@router.delete("/brands/{brand_id}/site", response_model=SiteStatusResponse)
async def disconnect_site(brand_id: str) -> SiteStatusResponse:
    brand = await Brand.get(brand_id)
    site = await Site.any_for_brand(brand.id)
    if site is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no site to disconnect")
    site = await Site.upsert_for_brand(brand.id, site.platform, STATUS_DISCONNECTED, credentials_encrypted=None)
    return _site_status(site)


def _site_status(site: Site) -> SiteStatusResponse:
    return SiteStatusResponse.create({"platform": site.platform, "status": site.status, "connected_at": site.connected_at.isoformat() if site.connected_at else None})


def _normalized_credentials(platform: str, credentials: dict) -> dict:
    cleaned = {key: str(credentials.get(key) or "").strip() for key in REQUIRED_CREDENTIAL_KEYS[platform]}
    missing = [key for key, value in cleaned.items() if not value]
    if missing:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"missing credential field(s): {', '.join(missing)}")
    if platform == PLATFORM_WORDPRESS:
        base_url = cleaned["base_url"].rstrip("/")
        cleaned["base_url"] = base_url if base_url.startswith("http") else f"https://{base_url}"
    if platform == PLATFORM_SHOPIFY:
        cleaned["shop_domain"] = re.sub(r"^https?://", "", cleaned["shop_domain"]).strip("/")
    return cleaned

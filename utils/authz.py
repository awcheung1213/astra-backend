"""Authentication + authorization as ONE FastAPI dependency, the geoviz
pattern adapted to astra's flat resource URLs.

geoviz nests every tenant route under /organizations/{organization_id}/ and
guards on that path param. Astra's routes are brand-first
(/brands/{brand_id}/...) with a handful of other resource shapes, so the
single `guard` dependency (attached router-level in main.py):

1. authenticates the Supabase bearer token (401 if missing/invalid) and
   JIT-provisions the users row from its claims;
2. finds the tenant the route touches from whichever ownership-bearing path
   param is present — organization_id directly, brand_id via the brand,
   artifact_id/report_id/change_set_id via their brand — and 403s unless it
   is the caller's organization (a user belongs to exactly ONE org, held on
   the users row). Unknown ids 404 via the objects' own *NotFound. A route
   with none of these params (GET /brands, /me, signup) is authenticated
   only — its handler scopes by the caller itself via current_user().

No role/permission machinery on purpose: every user is their org's admin
(objects/user.py ROLE_ADMIN) until an invite flow creates a second kind of
seat — geoviz's ROLE_PERMISSIONS expansion returns here with that flow.
401 = not authenticated. 403 = authenticated, not allowed.

Deliberately PUBLIC (registered without the guard in main.py): the Google
OAuth callback (HMAC-signed state IS the auth — Google's redirect carries no
bearer token) and the review webhook (signature verification is Phase 5)."""

from fastapi import HTTPException, Request, status

from objects.artifact import Artifact
from objects.brand import Brand
from objects.change_set import ChangeSet
from objects.report import Report
from objects.user import User
from utils.identity import AUTH
from utils.token_verifier import Principal


async def guard(request: Request) -> User:
    """Router-level dependency: authenticate, JIT-provision, enforce that the
    route's resource belongs to the caller's org. Caches the user on
    request.state for handlers (current_user)."""
    principal: Principal = await AUTH.current_principal(request)
    user = await User.upsert_from_claims(auth_sub=principal.sub, email=principal.email or f"{principal.sub}@unknown.invalid")
    request.state.current_user = user

    organization_id = await _owning_organization_id(request)
    if organization_id is not None and organization_id != user.organization_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not your organization")
    return user


def current_user(request: Request) -> User:
    """The guard's JIT-provisioned caller, for handlers that scope by the
    caller rather than a path param (GET /brands, /me, signup)."""
    return request.state.current_user


async def _owning_organization_id(request: Request) -> str | None:
    """The organization that owns whatever resource the path names, or None
    when the route carries no ownership-bearing param."""
    params = request.path_params
    if "organization_id" in params:
        return params["organization_id"]
    if "brand_id" in params:
        return (await Brand.get(params["brand_id"])).organization_id
    if "artifact_id" in params:
        return (await Brand.get((await Artifact.get(params["artifact_id"])).brand_id)).organization_id
    if "change_set_id" in params:
        return (await Brand.get((await ChangeSet.get(params["change_set_id"])).brand_id)).organization_id
    if "report_id" in params:
        report = await Report.get(params["report_id"])
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report not found")
        return (await Brand.get(report.brand_id)).organization_id
    return None

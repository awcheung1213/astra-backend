"""Who am I — the FE's session bootstrap. Rides the router-level guard like
everything else (the guard JIT-provisions the users row from the token, so
a fresh Supabase account's first /me call is what creates it here).
`organization_id: null` = hasn't completed signup; `onboarded: false` =
signup happened but no brand has launched — both route the FE into the
onboarding flow."""

from fastapi import APIRouter, Request

from objects.brand import Brand
from schemas.me_schemas import MeResponse
from utils.authz import current_user

router = APIRouter(prefix="/api/v1")


@router.get("/me", response_model=MeResponse)
async def me(request: Request) -> MeResponse:
    user = current_user(request)
    brands = await Brand.by_organization_id(user.organization_id) if user.organization_id else []
    return MeResponse.create(
        {
            "id": user.id,
            "email": user.email,
            "organization_id": user.organization_id,
            "role": user.role,
            "brand_id": brands[0].id if brands else None,
            "onboarded": any(brand.onboarded_at is not None for brand in brands),
        }
    )

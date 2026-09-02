"""Brand update — the "client-confirmed" half of the interview extraction
(schemas/onboarding_schemas.py promised edits would ride this endpoint), and
the general facts-editing surface (name, site, description, category,
audience, differentiators). PATCH semantics: only fields present in the
request are applied; the schema's extra="forbid" is the whitelist.

NO AUTH YET (repo-wide) — same posture as the other routers."""

from fastapi import APIRouter, HTTPException, status

from objects.approval_setting import PRESET_AUTO_TASKS, ApprovalSetting
from objects.brand import Brand
from schemas.brand_schemas import ApprovalPresetRequest, ApprovalPresetResponse, BrandResponse, BrandUpdateRequest

router = APIRouter(prefix="/api/v1")


@router.patch("/brands/{brand_id}", response_model=BrandResponse)
async def update_brand(brand_id: str, request: BrandUpdateRequest) -> BrandResponse:
    brand = await Brand.get(brand_id)
    fields = request.model_dump(exclude_unset=True)

    if "name" in fields:
        fields["name"] = (fields["name"] or "").strip()
        if not fields["name"]:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name cannot be empty")
    if "website_url" in fields:
        website_url = (fields["website_url"] or "").strip()
        if not website_url:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="website_url cannot be empty")
        if not website_url.startswith("http"):
            website_url = f"https://{website_url}"
        fields["website_url"] = website_url
    if "differentiators" in fields:
        fields["differentiators"] = [differentiator.strip() for differentiator in (fields["differentiators"] or []) if differentiator and differentiator.strip()]

    if fields:
        brand = await Brand.update(brand.id, **fields)
    return BrandResponse.create(
        {
            "id": brand.id,
            "organization_id": brand.organization_id,
            "name": brand.name,
            "website_url": brand.website_url,
            "platform": brand.platform,
            "footprint": brand.footprint,
            "description": brand.description,
            "category": brand.category,
            "audience": brand.audience,
            "differentiators": brand.differentiators,
            "approval_preset": brand.approval_preset,
        }
    )


@router.put("/brands/{brand_id}/approval_preset", response_model=ApprovalPresetResponse)
async def set_approval_preset(brand_id: str, request: ApprovalPresetRequest) -> ApprovalPresetResponse:
    """Change how much ships without review ("relax it after the first
    month"). PUT semantics on purpose: the preset macro REPLACES the brand's
    approval_settings rows wholesale, so the call is idempotent. The
    always-approve floor is code (utils/approval_flow.py) — no preset ever
    auto-ships outward work."""
    brand = await Brand.get(brand_id)
    if request.approval_preset not in PRESET_AUTO_TASKS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"approval_preset must be one of {sorted(PRESET_AUTO_TASKS)}")
    auto_tasks = await ApprovalSetting.apply_preset(brand.id, request.approval_preset)
    await Brand.update(brand.id, approval_preset=request.approval_preset)
    return ApprovalPresetResponse.create({"brand_id": brand.id, "approval_preset": request.approval_preset, "auto_approved_tasks": auto_tasks})

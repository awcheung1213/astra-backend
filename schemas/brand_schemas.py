from pydantic import ConfigDict

from schemas.base_schemas import BaseRequest, BaseResponse


class BrandUpdateRequest(BaseRequest):
    """PATCH semantics: only fields present in the request are applied.
    extra="forbid" makes the whitelist loud — anything this schema doesn't
    carry (platform, footprint, onboarded_at, ...) is a 422, not a silent
    no-op. platform is the site connect flow's to set; footprint changes
    have market implications and get their own endpoint when needed."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    website_url: str | None = None
    description: str | None = None
    category: str | None = None
    audience: str | None = None
    differentiators: list[str] | None = None


class BrandResponse(BaseResponse):
    id: str
    organization_id: str
    name: str
    website_url: str
    platform: str
    footprint: str | None
    description: str | None
    category: str | None
    audience: str | None
    differentiators: list[str]
    approval_preset: str | None


class ApprovalPresetRequest(BaseRequest):
    approval_preset: str  # review_everything | balanced | hands_off


class ApprovalPresetResponse(BaseResponse):
    brand_id: str
    approval_preset: str
    auto_approved_tasks: list[str]  # the task names the macro set to auto

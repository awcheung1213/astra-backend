from schemas.base_schemas import BaseResponse


class MeResponse(BaseResponse):
    id: str
    email: str
    organization_id: str | None  # null = hasn't completed signup yet
    role: str
    brand_id: str | None  # the org's brand (astra: one brand per org today)
    onboarded: bool  # a brand has launched — the FE's dashboard-vs-wizard gate

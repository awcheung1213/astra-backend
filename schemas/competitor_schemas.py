from schemas.base_schemas import BaseRequest, BaseResponse


class CompetitorAddRequest(BaseRequest):
    name: str


class CompetitorItem(BaseResponse):
    id: str
    name: str
    is_active: bool


class CompetitorListResponse(BaseResponse):
    brand_id: str
    competitors: list[CompetitorItem]


class CompetitorRemoveResponse(BaseResponse):
    id: str
    is_excluded: bool

from pydantic import Field

from schemas.base_schemas import BaseResponse


class IndexResponse(BaseResponse):
    project: str = Field(examples=["evergreen-backend"])
    environment: str = Field(examples=["local"])
    region: str = Field(examples=["us-east-1"])
    version: str = Field(examples=["0.1.0"])
    timestamp: str = Field(examples=["2026-08-19T00:00:00.000000"])

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject

KIND_CALL = "call"
KIND_FORM = "form"
KIND_ORDER = "order"


class Lead(SQLModel, BaseObject, table=True):
    """One conversions ledger: local reads counts, stores read dollars.
    landing_path is the 'built by Astra' attribution join — indexed."""

    __tablename__ = "leads"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    kind: str  # call | form | order
    value_usd: Decimal | None = Field(default=None, sa_column=Column(Numeric(12, 2), nullable=True))
    source_channel: str  # organic | ai_referral | gbp | shopping | direct
    source_detail: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # query text, engine name, referrer
    landing_path: str | None = Field(default=None, sa_column=Column(String, nullable=True, index=True))
    occurred_at: datetime = Field(sa_column=Column(DateTime, nullable=False))
    meta: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

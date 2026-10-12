import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database


class BrandMarket(SQLModel, BaseObject, table=True):
    """Subscription join. source = the maintenance rule: the system may
    re-sample its own `sampled` rows, never a human's (`user`/`suggested`)."""

    __tablename__ = "brand_markets"
    __table_args__ = (UniqueConstraint("brand_id", "market_id"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    market_id: str = Field(sa_column=Column(String, ForeignKey("markets.id"), nullable=False, index=True))
    source: str = "user"  # user | suggested | sampled
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def subscribe(brand_id: str, market_id: str, source: str = "user") -> "BrandMarket":
        subscription = BrandMarket(brand_id=brand_id, market_id=market_id, source=source)
        Database.session.add(subscription)
        await Database.session.flush()
        return subscription

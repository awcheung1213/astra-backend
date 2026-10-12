import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func, select
from sqlmodel import Field, SQLModel

from exceptions.exceptions import ReviewNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase


class Review(SQLModel, BaseObject, table=True):
    __tablename__ = "reviews"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    platform: str  # google | trustpilot | g2 | yelp
    external_id: str
    rating: int = Field(sa_column=Column(Integer, nullable=False))
    author: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    text: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    received_at: datetime = Field(sa_column=Column(DateTime, nullable=False))
    # the drafted reply is an artifact; this row just points at it
    reply_artifact_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("artifacts.id"), nullable=True))
    replied_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def get(id: str) -> "Review":
        review = await ReplicaDatabase.session.get(Review, id)
        if not review:
            raise ReviewNotFound()
        return review

    @staticmethod
    async def get_by_external_id(brand_id: str, platform: str, external_id: str) -> "Review | None":
        """The dedupe identity — a re-delivered webhook finds its row."""
        rows = await ReplicaDatabase.session.execute(select(Review).where(Review.brand_id == brand_id).where(Review.platform == platform).where(Review.external_id == external_id).limit(1))
        return rows.scalars().first()

    @staticmethod
    async def create(brand_id: str, platform: str, external_id: str, rating: int, received_at: datetime, author: str | None = None, text: str | None = None) -> "Review":
        review = Review(brand_id=brand_id, platform=platform, external_id=external_id, rating=rating, received_at=received_at, author=author, text=text)
        session = Database.session
        session.add(review)
        await session.flush()
        await session.refresh(review)
        return review

    @staticmethod
    async def mark_replied(id: str) -> None:
        """Stamp the moment the approved reply actually went live on the
        platform — the gbp_apply idempotency guard."""
        review = await Database.session.get(Review, id)
        if review is None:
            raise ReviewNotFound()
        review.replied_at = clock.now()
        Database.session.add(review)
        await Database.session.flush()

    @staticmethod
    async def link_reply(id: str, reply_artifact_id: str) -> None:
        """Point the review at its drafted reply. replied_at stays NULL until
        the approved reply actually posts (the GBP adapter's job)."""
        review = await Database.session.get(Review, id)
        if review is None:
            raise ReviewNotFound()
        review.reply_artifact_id = reply_artifact_id
        Database.session.add(review)
        await Database.session.flush()

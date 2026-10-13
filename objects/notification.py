import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

KIND_WIN = "win"
KIND_ALERT = "alert"
KIND_UPDATE = "update"

URGENCY_NORMAL = "normal"
URGENCY_URGENT = "urgent"  # urgent -> SMS if enabled; everything -> Pulse


class Notification(SQLModel, BaseObject, table=True):
    __tablename__ = "notifications"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str = Field(sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True))
    kind: str  # win | alert | update
    urgency: str = Field(default=URGENCY_NORMAL, sa_column=Column(String, server_default=URGENCY_NORMAL, nullable=False))
    title: str
    body: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    link_ref: str | None = Field(default=None, sa_column=Column(String, nullable=True))  # deep link into the app (e.g. tasks/<id>)
    channels_sent: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    read_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

    @staticmethod
    async def create(
        organization_id: str,
        kind: str,
        title: str,
        body: str | None = None,
        urgency: str = URGENCY_NORMAL,
        link_ref: str | None = None,
    ) -> "Notification":
        notification = Notification(organization_id=organization_id, kind=kind, title=title, body=body, urgency=urgency, link_ref=link_ref)
        session = Database.session
        session.add(notification)
        await session.flush()
        await session.refresh(notification)
        return notification

    @staticmethod
    async def unread_change_notice(organization_id: str, since: datetime) -> "Notification | None":
        """The newest still-unread change notice since `since` — a new change folds into it."""
        rows = await Database.session.execute(
            select(Notification)
            .where(Notification.organization_id == organization_id, Notification.kind == KIND_UPDATE, Notification.read_at.is_(None), Notification.created_at >= since)  # type: ignore
            .where(Notification.link_ref.like("change_sets/%") | Notification.link_ref.like("changes/%"))  # type: ignore
            .order_by(Notification.created_at.desc())  # type: ignore
            .limit(1)
        )
        return rows.scalars().first()

    @staticmethod
    async def mark_all_read(organization_id: str) -> int:
        """Opening Pulse reads it: the badge clears."""
        result = await Database.session.execute(update(Notification).where(Notification.organization_id == organization_id, Notification.read_at.is_(None)).values(read_at=clock.now()))  # type: ignore
        await Database.session.flush()
        return result.rowcount or 0

    @staticmethod
    async def for_organization(organization_id: str, limit: int = 50) -> list["Notification"]:
        """Newest-first — the Pulse feed."""
        rows = await ReplicaDatabase.session.execute(select(Notification).where(Notification.organization_id == organization_id).order_by(Notification.created_at.desc()).limit(limit))  # type: ignore
        return list(rows.scalars().all())

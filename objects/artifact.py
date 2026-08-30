import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import ArtifactNotFound
from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase

STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_AUTO_APPROVED = "auto_approved"
STATUS_REJECTED = "rejected"


class Artifact(SQLModel, BaseObject, table=True):
    """Every agent/pipeline output. Only site-touching types convert to change sets."""

    __tablename__ = "artifacts"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    task_id: str = Field(sa_column=Column(String, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True))
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    type: str  # change_proposal | draft | pitch | reply | flag_request | diagnosis | plan | brief
    content: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    evidence: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}"))
    status: str = Field(default=STATUS_PENDING_REVIEW, sa_column=Column(String, server_default=STATUS_PENDING_REVIEW, nullable=False))
    reviewed_by: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    reviewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def create(
        task_id: str,
        brand_id: str,
        type: str,
        content: dict,
        evidence: dict | None = None,
        status: str = STATUS_PENDING_REVIEW,
    ) -> "Artifact":
        artifact = Artifact(task_id=task_id, brand_id=brand_id, type=type, content=content, evidence=evidence or {}, status=status)
        session = Database.session
        session.add(artifact)
        await session.flush()
        await session.refresh(artifact)
        return artifact

    @staticmethod
    async def get(id: str) -> "Artifact":
        artifact = await ReplicaDatabase.session.get(Artifact, id)
        if not artifact:
            raise ArtifactNotFound()
        return artifact

    @staticmethod
    async def review(id: str, status: str, reviewed_by: str | None = None) -> "Artifact":
        """Stamp the human verdict (approved | rejected) + who and when."""
        artifact = await Database.session.get(Artifact, id)
        if not artifact:
            raise ArtifactNotFound()
        artifact.status = status
        artifact.reviewed_by = reviewed_by
        artifact.reviewed_at = datetime.now()
        Database.session.add(artifact)
        await Database.session.flush()
        await Database.session.refresh(artifact)
        return artifact

    @staticmethod
    async def latest(brand_id: str, type: str, statuses: list[str] | None = None) -> "Artifact | None":
        """Newest artifact of a type for a brand, optionally filtered to given
        statuses (e.g. only approved plans)."""
        statement = select(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.type == type).order_by(Artifact.created_at.desc()).limit(1)  # type: ignore
        if statuses:
            statement = statement.where(Artifact.status.in_(statuses))  # type: ignore
        rows = await ReplicaDatabase.session.execute(statement)
        return rows.scalars().first()

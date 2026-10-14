import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import ArtifactNotFound
from objects.base_object import BaseObject
from utils import clock
from utils.database import Database, ReplicaDatabase

STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_AUTO_APPROVED = "auto_approved"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"  # retired unreviewed by a newer artifact of its type (DEC-018); out of the approvals queue


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
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, default=clock.column_default(), server_default=func.now(), nullable=False))

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
    async def get_for_update(id: str) -> "Artifact":
        """The artifact row LOCKED (SELECT ... FOR UPDATE) until the request's
        transaction ends — concurrent verdicts serialize here, so the loser
        re-reads the winner's status instead of approving twice."""
        rows = await Database.session.execute(select(Artifact).where(Artifact.id == id).with_for_update().execution_options(populate_existing=True))  # type: ignore[arg-type]
        artifact = rows.scalars().first()
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
        artifact.reviewed_at = clock.now()
        Database.session.add(artifact)
        await Database.session.flush()
        await Database.session.refresh(artifact)
        return artifact

    @staticmethod
    async def set_content(id: str, content: dict, status: str | None = None) -> "Artifact":
        """Replace the content (a NEW dict, so the jsonb change is detected) and
        optionally move the status — the pitch-send state machine's writer.
        Returning to pending_review clears the verdict stamp."""
        artifact = await Database.session.get(Artifact, id)
        if not artifact:
            raise ArtifactNotFound()
        artifact.content = dict(content)
        if status is not None:
            artifact.status = status
            if status == STATUS_PENDING_REVIEW:
                artifact.reviewed_by = None
                artifact.reviewed_at = None
        Database.session.add(artifact)
        await Database.session.flush()
        await Database.session.refresh(artifact)
        return artifact

    @staticmethod
    async def pending_for_brand(brand_id: str) -> list["Artifact"]:
        """The approvals queue, oldest first — review in arrival order."""
        rows = await ReplicaDatabase.session.execute(select(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.status == STATUS_PENDING_REVIEW).order_by(Artifact.created_at))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def for_brand(brand_id: str, type: str, statuses: list[str]) -> list["Artifact"]:
        """Every artifact of a type for a brand in the given statuses."""
        rows = await ReplicaDatabase.session.execute(select(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.type == type).where(Artifact.status.in_(statuses)))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def by_ids(ids) -> list["Artifact"]:
        ids = [artifact_id for artifact_id in set(ids) if artifact_id]
        if not ids:
            return []
        rows = await ReplicaDatabase.session.execute(select(Artifact).where(Artifact.id.in_(ids)))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def created_since(brand_id: str, since: datetime) -> list["Artifact"]:
        """Every artifact born since `since`, newest first — the done-for-you feed's raw material."""
        rows = await ReplicaDatabase.session.execute(select(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.created_at >= since).order_by(Artifact.created_at.desc()))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def supersede_pending(brand_id: str, type: str, keep_id: str) -> int:
        """Retire the brand's still-pending artifacts of a type other than
        keep_id (the newborn): a new plan replaces the one nobody reviewed
        (DEC-018). Returns how many were retired."""
        result = await Database.session.execute(update(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.type == type).where(Artifact.status == STATUS_PENDING_REVIEW).where(Artifact.id != keep_id).values(status=STATUS_SUPERSEDED, reviewed_at=clock.now()))  # type: ignore
        return result.rowcount or 0

    @staticmethod
    async def latest(brand_id: str, type: str, statuses: list[str] | None = None) -> "Artifact | None":
        """Newest artifact of a type for a brand, optionally filtered to given
        statuses (e.g. only approved plans)."""
        statement = select(Artifact).where(Artifact.brand_id == brand_id).where(Artifact.type == type).order_by(Artifact.created_at.desc()).limit(1)  # type: ignore
        if statuses:
            statement = statement.where(Artifact.status.in_(statuses))  # type: ignore
        rows = await ReplicaDatabase.session.execute(statement)
        return rows.scalars().first()

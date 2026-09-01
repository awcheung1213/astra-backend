import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from exceptions.exceptions import ChangeSetNotFound
from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase

STATUS_PENDING = "pending"
STATUS_APPLYING = "applying"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_REVERTED = "reverted"


class ChangeSet(SQLModel, BaseObject, table=True):
    """The only thing that writes to a client's site. Each operation carries an
    id + completion marker so a retried apply RESUMES, never repeats.
    Append-only: a revert is a NEW change set pointed at by reverted_by_change_set_id."""

    __tablename__ = "change_sets"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    artifact_id: str = Field(sa_column=Column(String, ForeignKey("artifacts.id"), nullable=False, index=True))
    site_id: str = Field(sa_column=Column(String, ForeignKey("sites.id"), nullable=False, index=True))
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    status: str = Field(default=STATUS_PENDING, sa_column=Column(String, server_default=STATUS_PENDING, nullable=False))
    operations: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False, server_default="[]"))
    applied_at: datetime | None = Field(default=None, sa_column=Column(DateTime, nullable=True))
    reverted_by_change_set_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("change_sets.id"), nullable=True))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def create(artifact_id: str, site_id: str, brand_id: str, operations: list[dict]) -> "ChangeSet":
        change_set = ChangeSet(artifact_id=artifact_id, site_id=site_id, brand_id=brand_id, operations=operations)
        session = Database.session
        session.add(change_set)
        await session.flush()
        await session.refresh(change_set)
        return change_set

    @staticmethod
    async def get(id: str) -> "ChangeSet":
        change_set = await Database.session.get(ChangeSet, id)
        if not change_set:
            raise ChangeSetNotFound()
        return change_set

    @staticmethod
    async def update_status(id: str, status: str) -> None:
        change_set = await Database.session.get(ChangeSet, id)
        if change_set is None:
            raise ChangeSetNotFound()
        change_set.status = status
        if status == STATUS_APPLIED:
            change_set.applied_at = datetime.now()
        Database.session.add(change_set)
        await Database.session.flush()

    @staticmethod
    async def for_brand(brand_id: str, limit: int = 50) -> list["ChangeSet"]:
        """Newest-first — the shipped-changes ledger."""
        rows = await ReplicaDatabase.session.execute(select(ChangeSet).where(ChangeSet.brand_id == brand_id).order_by(ChangeSet.created_at.desc()).limit(limit))  # type: ignore
        return list(rows.scalars().all())

    @staticmethod
    async def mark_reverted(id: str, reverted_by_change_set_id: str) -> None:
        """Link the original to the revert set that undoes it (append-only:
        the original row itself is never edited beyond status + link)."""
        change_set = await ChangeSet.get(id)
        change_set.status = STATUS_REVERTED
        change_set.reverted_by_change_set_id = reverted_by_change_set_id
        Database.session.add(change_set)
        await Database.session.flush()

    @staticmethod
    async def mark_operation_complete(id: str, operation_id: str, apply_result: dict) -> None:
        """Stamp ONE operation's completion marker + what the adapter reported:
        previous_value (the rollback record) and created_ref (anything the op
        created — how a publish gets unpublished). Assigns a NEW list so the
        JSONB change is detected. The caller commits — the marker must survive
        a later op's failure, or a retried apply would repeat this op."""
        change_set = await ChangeSet.get(id)
        updated_operations = []
        for operation in change_set.operations:
            if operation.get("id") == operation_id:
                operation = {**operation, "previous_value": apply_result.get("previous_value"), "created_ref": apply_result.get("created_ref"), "completed_at": datetime.now().isoformat()}
            updated_operations.append(operation)
        change_set.operations = updated_operations
        Database.session.add(change_set)
        await Database.session.flush()

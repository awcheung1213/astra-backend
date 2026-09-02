import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint, delete, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase

MODE_AUTO = "auto"
MODE_APPROVE = "approve"

PRESET_REVIEW_EVERYTHING = "review_everything"
PRESET_BALANCED = "balanced"
PRESET_HANDS_OFF = "hands_off"

# preset -> the producing task names whose artifacts ship without review.
# Presets are display-level macros: applying one REWRITES the brand's rows,
# and the rows are what initial_artifact_status consults. The always-approve
# floor (utils/approval_flow.py) is enforced in code either way — these names
# only ever cover site-changing work the change-set engine can roll back.
PRESET_AUTO_TASKS: dict[str, tuple[str, ...]] = {
    PRESET_REVIEW_EVERYTHING: (),
    PRESET_BALANCED: ("technical_fix",),
    PRESET_HANDS_OFF: ("technical_fix", "content_draft", "content_calendar"),
}


class ApprovalSetting(SQLModel, BaseObject, table=True):
    """Presets are macros that write these rows; the always-approve floor
    (outward/irreversible artifact types) is enforced in code, never a row."""

    __tablename__ = "approval_settings"
    __table_args__ = (UniqueConstraint("brand_id", "task_name"),)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    brand_id: str = Field(sa_column=Column(String, ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True))
    task_name: str
    mode: str = Field(default=MODE_APPROVE, sa_column=Column(String, server_default=MODE_APPROVE, nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def apply_preset(brand_id: str, preset: str) -> list[str]:
        """The preset macro: replace the brand's rows wholesale with the
        preset's MODE_AUTO set (default mode is approve, so absent = reviewed).
        Presets are the only writer today, which is what makes replace safe."""
        await Database.session.execute(delete(ApprovalSetting).where(ApprovalSetting.brand_id == brand_id))  # type: ignore
        auto_tasks = PRESET_AUTO_TASKS[preset]
        for task_name in auto_tasks:
            Database.session.add(ApprovalSetting(brand_id=brand_id, task_name=task_name, mode=MODE_AUTO))
        await Database.session.flush()
        return list(auto_tasks)

    @staticmethod
    async def mode_for(brand_id: str, task_name: str) -> str:
        rows = await ReplicaDatabase.session.execute(select(ApprovalSetting).where(ApprovalSetting.brand_id == brand_id).where(ApprovalSetting.task_name == task_name))  # type: ignore
        setting = rows.scalars().first()
        return setting.mode if setting else MODE_APPROVE

"""Users — the geoviz identity pattern collapsed onto evergreen's one-org-per-
user model: no separate memberships table, the org pointer + role live on
the users row (NULL organization_id = signed up in Supabase but hasn't
completed signup here yet — the onboarding gate). We key our OWN id and
treat the IdP subject (supabase_user_id) as an attribute. No password_hash
anywhere: credentials live exclusively in Supabase Auth."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, func, select
from sqlmodel import Field, SQLModel

from objects.base_object import BaseObject
from utils.database import Database, ReplicaDatabase

ROLE_ADMIN = "admin"  # the only role today — every user claims their own org; more roles arrive with an invite flow


class User(SQLModel, BaseObject, table=True):
    __tablename__ = "users"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    organization_id: str | None = Field(default=None, sa_column=Column(String, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True))
    supabase_user_id: str = Field(sa_column=Column(String, nullable=False, unique=True))
    email: str
    role: str = ROLE_ADMIN
    phone: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    sms_urgent_enabled: bool = Field(default=False, sa_column=Column(Boolean, server_default="false", nullable=False))
    created_at: datetime = Field(default=None, sa_column=Column(DateTime, server_default=func.now(), nullable=False))

    @staticmethod
    async def get_by_sub(supabase_user_id: str) -> "User | None":
        rows = await ReplicaDatabase.session.execute(select(User).where(User.supabase_user_id == supabase_user_id))  # type: ignore
        return rows.scalars().first()

    @staticmethod
    async def upsert_from_claims(auth_sub: str, email: str) -> "User":
        """JIT provisioning: the first valid token creates the row (org-less,
        which is what routes the FE into onboarding); later logins keep email
        in sync (Supabase is authoritative for identity attributes, we're
        authoritative for the org pointer)."""
        email = email.strip().lower()
        session = Database.session
        user = await User.get_by_sub(auth_sub)
        if user is None:
            user = User(supabase_user_id=auth_sub, email=email)
            session.add(user)
            await session.flush()
            await session.refresh(user)
            return user
        if user.email != email:
            user.email = email
            session.add(user)
            await session.flush()
        return user

    @staticmethod
    async def claim_organization(id: str, organization_id: str, role: str = ROLE_ADMIN) -> "User":
        user = await Database.session.get(User, id)
        user.organization_id = organization_id
        user.role = role
        Database.session.add(user)
        await Database.session.flush()
        await Database.session.refresh(user)
        return user

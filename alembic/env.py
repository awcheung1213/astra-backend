from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from alembic import context
from objects.approval_setting import ApprovalSetting  # noqa
from objects.artifact import Artifact  # noqa
from objects.batch import Batch  # noqa
from objects.brand import Brand  # noqa
from objects.brand_market import BrandMarket  # noqa
from objects.brand_service import BrandService  # noqa
from objects.change_set import ChangeSet  # noqa
from objects.citation import Citation  # noqa
from objects.competitor import Competitor  # noqa
from objects.competitor_mention import CompetitorMention  # noqa
from objects.connection import Connection  # noqa
from objects.cost_event import CostEvent  # noqa
from objects.finding import Finding  # noqa
from objects.goal import Goal  # noqa
from objects.lead import Lead  # noqa
from objects.market import Market  # noqa
from objects.metric_point import MetricPoint  # noqa
from objects.notification import Notification  # noqa
from objects.organization import Organization  # noqa
from objects.prompt import Prompt  # noqa
from objects.prompt_result import PromptResult  # noqa
from objects.query_response import QueryResponse  # noqa
from objects.report import Report  # noqa
from objects.review import Review  # noqa
from objects.site import Site  # noqa
from objects.site_page import SitePage  # noqa
from objects.snapshot import Snapshot  # noqa
from objects.task import Task  # noqa
from objects.user import User  # noqa
from objects.visibility_run import VisibilityRun  # noqa
from objects.visibility_run_market import VisibilityRunMarket  # noqa

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Point Alembic at whatever database the app resolved for this ENVIRONMENT
# (settings.DATABASE_* — from individual env vars), overriding the local default
# hardcoded in alembic.ini. Same pg8000 driver, same DB the app talks to — so
# migrations hit the right Postgres on Railway too.

import settings  # noqa: E402

config.set_main_option(
    "sqlalchemy.url",
    f"postgresql+pg8000://{settings.DATABASE_USERNAME}:{settings.DATABASE_PASSWORD}@{settings.DATABASE_HOST}:{settings.DATABASE_PORT}/{settings.DATABASE_NAME}",
)

target_metadata = SQLModel.metadata


def include_object(object, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate's hands off procrastinate's schema.

    The queue library owns its own tables/indexes (procrastinate_jobs, _workers,
    _events, _periodic_defers, ...), applied via `procrastinate schema --apply` —
    they live in the database but not in SQLModel.metadata, so without this
    filter autogenerate 'detects' them as removed and emits DROP TABLE for the
    entire queue infrastructure.
    """
    if name and name.startswith("procrastinate"):
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

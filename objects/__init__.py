"""Import every model so the SQLAlchemy registry is COMPLETE in any process
that touches one of them. Without this, a process whose import graph skips a
model (e.g. the worker never importing Organization) fails FK resolution at
runtime ("could not find table 'organizations'") the first time a mapper
configures. Alembic's env.py relies on the same completeness."""

from objects import approval_setting  # noqa: F401
from objects import artifact  # noqa: F401
from objects import batch  # noqa: F401
from objects import brand  # noqa: F401
from objects import brand_market  # noqa: F401
from objects import brand_service  # noqa: F401
from objects import change_set  # noqa: F401
from objects import citation  # noqa: F401
from objects import competitor  # noqa: F401
from objects import competitor_mention  # noqa: F401
from objects import connection  # noqa: F401
from objects import cost_event  # noqa: F401
from objects import finding  # noqa: F401
from objects import goal  # noqa: F401
from objects import lead  # noqa: F401
from objects import market  # noqa: F401
from objects import metric_point  # noqa: F401
from objects import notification  # noqa: F401
from objects import organization  # noqa: F401
from objects import prompt  # noqa: F401
from objects import prompt_result  # noqa: F401
from objects import query_response  # noqa: F401
from objects import report  # noqa: F401
from objects import review  # noqa: F401
from objects import site  # noqa: F401
from objects import snapshot  # noqa: F401
from objects import task  # noqa: F401
from objects import user  # noqa: F401
from objects import visibility_run  # noqa: F401
from objects import visibility_run_market  # noqa: F401

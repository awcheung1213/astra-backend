import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings.local import *  # noqa

environment = os.environ.get("ENVIRONMENT") or "local"
if environment not in ["local", "staging", "production"]:
    environment = "local"

exec(f"from settings.{environment} import *")

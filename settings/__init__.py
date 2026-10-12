import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from settings.local import *  # noqa

environment = os.environ.get("ENVIRONMENT") or "local"
if environment not in ["local", "staging", "production"]:
    environment = "local"

# Deployed (Railway injects RAILWAY_ENVIRONMENT) but ENVIRONMENT unset/unknown:
# refuse to boot rather than silently run local settings — the docker DB host,
# the dev encryption key, and wildcard CORS.
if environment == "local" and os.environ.get("RAILWAY_ENVIRONMENT"):
    raise RuntimeError(f"ENVIRONMENT must be 'staging' or 'production' on Railway (got {os.environ.get('ENVIRONMENT')!r}); set it in the service's variables")

exec(f"from settings.{environment} import *")

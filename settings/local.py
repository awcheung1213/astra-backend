import base64

from settings.base import *  # noqa

# --- database: the local docker-compose Postgres container (hardcoded) ---
DATABASE_HOST = "astra-backend-database"
REPLICA_DATABASE_HOST = "astra-backend-database"
DATABASE_NAME = "postgres"
DATABASE_USERNAME = "postgres"
DATABASE_PASSWORD = "postgres"
DATABASE_PORT = 5432

# --- app config ---
CORS_ORIGINS = ["*"]  # any origin — local dev only

# --- credentials crypto (sites/connections credentials_encrypted) ---
# fixed dev key — real environments require their own via env var
TOKEN_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"astra-local-dev-key-0123456789ab").decode()

TEST_URL = "http://astra-backend:8000"

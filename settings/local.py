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

TEST_URL = "http://astra-backend:8000"

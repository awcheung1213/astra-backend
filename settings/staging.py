import os

from settings.base import *  # noqa

# --- database (Railway Postgres) ---
DATABASE_HOST = os.environ["DATABASE_HOST"]
REPLICA_DATABASE_HOST = os.environ.get("REPLICA_DATABASE_HOST") or os.environ["DATABASE_HOST"]
DATABASE_NAME = os.environ["DATABASE_NAME"]
DATABASE_USERNAME = os.environ["DATABASE_USERNAME"]
DATABASE_PASSWORD = os.environ["DATABASE_PASSWORD"]
DATABASE_PORT = int(os.environ.get("DATABASE_PORT", "5432"))

# --- app config ---
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",") if origin.strip()]

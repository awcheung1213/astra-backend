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
CORS_ORIGINS = [origin.strip() for origin in os.environ["CORS_ORIGINS"].split(",") if origin.strip()]
assert CORS_ORIGINS and "*" not in CORS_ORIGINS, "production requires explicit CORS_ORIGINS"

# --- credentials crypto (sites/connections credentials_encrypted) ---
TOKEN_ENCRYPTION_KEY = os.environ["TOKEN_ENCRYPTION_KEY"]

# --- auth (Supabase project; verification fails closed without it) ---
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
# no test-runner credentials in production on purpose
TEST_RUNNER_EMAIL = ""
TEST_RUNNER_PASSWORD = ""

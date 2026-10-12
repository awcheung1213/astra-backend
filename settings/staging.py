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

# --- credentials crypto (sites/connections credentials_encrypted) ---
TOKEN_ENCRYPTION_KEY = os.environ["TOKEN_ENCRYPTION_KEY"]

# --- auth (Supabase project; verification fails closed without it) ---
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
TEST_RUNNER_EMAIL = os.environ.get("TEST_RUNNER_EMAIL", "")
TEST_RUNNER_PASSWORD = os.environ.get("TEST_RUNNER_PASSWORD", "")

# --- review webhook HMAC (X-Evergreen-Signature); unset = every delivery 401s (fails CLOSED) ---
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

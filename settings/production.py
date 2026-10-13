import os

from settings._env import env_int, env_required
from settings.base import *  # noqa

# no request bodies/headers in production logs unless LOG_LEVEL=DEBUG is set on purpose
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# --- database (Railway Postgres) ---
DATABASE_HOST = env_required("DATABASE_HOST")
REPLICA_DATABASE_HOST = os.environ.get("REPLICA_DATABASE_HOST") or env_required("DATABASE_HOST")
DATABASE_NAME = env_required("DATABASE_NAME")
DATABASE_USERNAME = env_required("DATABASE_USERNAME")
DATABASE_PASSWORD = env_required("DATABASE_PASSWORD")
DATABASE_PORT = env_int("DATABASE_PORT", 5432)

# --- app config ---
CORS_ORIGINS = [origin.strip() for origin in env_required("CORS_ORIGINS").split(",") if origin.strip()]
assert CORS_ORIGINS and "*" not in CORS_ORIGINS, "production requires explicit CORS_ORIGINS"

# --- credentials crypto (sites/connections credentials_encrypted) ---
TOKEN_ENCRYPTION_KEY = env_required("TOKEN_ENCRYPTION_KEY")

# --- auth (Supabase project; verification fails closed without it) ---
SUPABASE_URL = env_required("SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
# no test-runner credentials in production on purpose
TEST_RUNNER_EMAIL = ""
TEST_RUNNER_PASSWORD = ""

# --- review webhook HMAC (X-Evergreen-Signature); required — the webhook's only auth ---
WEBHOOK_SECRET = env_required("WEBHOOK_SECRET")

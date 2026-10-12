import os

from settings._env import env_int, env_required
from settings.base import *  # noqa

# --- database (Railway Postgres) ---
DATABASE_HOST = env_required("DATABASE_HOST")
REPLICA_DATABASE_HOST = os.environ.get("REPLICA_DATABASE_HOST") or env_required("DATABASE_HOST")
DATABASE_NAME = env_required("DATABASE_NAME")
DATABASE_USERNAME = env_required("DATABASE_USERNAME")
DATABASE_PASSWORD = env_required("DATABASE_PASSWORD")
DATABASE_PORT = env_int("DATABASE_PORT", 5432)

# --- app config ---
CORS_ORIGINS = [origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",") if origin.strip()]

# --- credentials crypto (sites/connections credentials_encrypted) ---
TOKEN_ENCRYPTION_KEY = env_required("TOKEN_ENCRYPTION_KEY")

# --- auth (Supabase project; verification fails closed without it) ---
SUPABASE_URL = env_required("SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
TEST_RUNNER_EMAIL = os.environ.get("TEST_RUNNER_EMAIL", "")
TEST_RUNNER_PASSWORD = os.environ.get("TEST_RUNNER_PASSWORD", "")

# --- review webhook HMAC (X-Evergreen-Signature); unset = every delivery 401s (fails CLOSED) ---
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

import base64
import os

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

# --- auth (Supabase project; the geoviz pattern: verify locally via JWKS) ---
# The sentinel default lets the app import/boot without a project — every
# request then 401s (auth fails CLOSED; there is no stub mode). Local dev +
# tests point at a real STAGING project via .env.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://test.invalid")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
# dedicated login-only Supabase account the test suite signs in with;
# auth-dependent tests SKIP when absent (tests/_auth.py)
TEST_RUNNER_EMAIL = os.environ.get("TEST_RUNNER_EMAIL", "")
TEST_RUNNER_PASSWORD = os.environ.get("TEST_RUNNER_PASSWORD", "")

TEST_URL = "http://astra-backend:8000"

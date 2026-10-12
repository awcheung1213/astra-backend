"""Settings refuse to fall back to local on a deploy (pure: subprocess imports, no DB)."""

import os
import subprocess
import sys


def _import_settings(env: dict) -> subprocess.CompletedProcess:
    base = {key: value for key, value in os.environ.items() if key not in ("ENVIRONMENT", "RAILWAY_ENVIRONMENT")}
    return subprocess.run([sys.executable, "-c", "import settings; print(settings.ENVIRONMENT)"], env={**base, **env}, capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_railway_without_environment_refuses_to_boot():
    result = _import_settings({"RAILWAY_ENVIRONMENT": "production"})
    assert result.returncode != 0
    assert "ENVIRONMENT must be 'staging' or 'production' on Railway" in result.stderr


def test_railway_with_unknown_environment_refuses_to_boot():
    result = _import_settings({"RAILWAY_ENVIRONMENT": "production", "ENVIRONMENT": "prod"})
    assert result.returncode != 0


def test_local_without_railway_still_defaults_to_local():
    result = _import_settings({})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("local")


PRODUCTION = {
    "ENVIRONMENT": "production",
    "RAILWAY_ENVIRONMENT": "production",
    "DATABASE_HOST": "db.internal",
    "DATABASE_NAME": "railway",
    "DATABASE_USERNAME": "postgres",
    "DATABASE_PASSWORD": "pw",
    "DATABASE_PORT": "5432",
    "CORS_ORIGINS": "https://evergreenofficial.app",
    "TOKEN_ENCRYPTION_KEY": "k",
    "SUPABASE_URL": "https://x.supabase.co",
    "WEBHOOK_SECRET": "s",
}


def test_production_boots_with_complete_variables():
    result = _import_settings(PRODUCTION)
    assert result.returncode == 0, result.stderr


def test_empty_database_port_falls_back_to_default():
    result = _import_settings({**PRODUCTION, "DATABASE_PORT": ""})
    assert result.returncode == 0, result.stderr


def test_unresolved_railway_reference_names_the_variable():
    # a ${{Postgres.PGHOST}} reference to a misnamed service resolves to ""
    result = _import_settings({**PRODUCTION, "DATABASE_HOST": ""})
    assert result.returncode != 0
    assert "DATABASE_HOST is missing or empty" in result.stderr

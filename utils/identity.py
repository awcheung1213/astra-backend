"""Auth wiring for astra-backend, ported from geoviz-backend: bearer tokens
are minted by Supabase Auth and verified locally via utils/token_verifier
against the project's JWKS asymmetric signing keys (deterministic single
path — no HS256/shared-secret fallback, no offline stub mode). Identity rows
live in THIS database (objects/user.py, resolved by utils/authz)."""

import settings
from utils.token_verifier import Auth, TokenVerifier

AUTH = Auth(
    TokenVerifier(
        supabase_url=settings.SUPABASE_URL,
    )
)

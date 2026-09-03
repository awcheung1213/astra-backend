from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request, status


class AuthConfigError(Exception):
    """The auth configuration is unusable. Raised at construction time so a
    misconfigured service crashes at boot instead of running insecure."""


class InvalidTokenError(Exception):
    """The bearer token failed verification (signature, expiry, audience,
    issuer, or algorithm). Deliberately one error type: callers map it to a
    401 and the detail stays out of responses (don't teach attackers which
    check failed)."""


@dataclass
class Principal:
    """The verified caller of a request: claims from a validated token."""

    sub: str
    email: str | None
    claims: dict

    @staticmethod
    def from_claims(claims: dict) -> "Principal":
        email = claims.get("email")
        return Principal(sub=claims["sub"], email=email.strip().lower() if email else None, claims=claims)

    @property
    def name(self) -> str | None:
        metadata = self.claims.get("user_metadata") or {}
        return metadata.get("name") or metadata.get("full_name") or self.claims.get("name")


class TokenVerifier:
    def __init__(
        self,
        supabase_url: str,
        jwks_client: Any | None = None,
        leeway_seconds: int = 30,  # absorbs container clock drift without meaningfully extending token life
    ) -> None:
        if not supabase_url:
            raise AuthConfigError("supabase_url is required, e.g. 'https://<project-ref>.supabase.co'")
        self._leeway = leeway_seconds
        self._issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self._jwks_client = jwks_client or jwt.PyJWKClient(f"{self._issuer}/.well-known/jwks.json", cache_keys=True)

    def verify(self, token: str) -> dict:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["ES256", "RS256", "EdDSA"],
                audience="authenticated",
                issuer=self._issuer,
                leeway=self._leeway,
            )
        except InvalidTokenError:
            raise
        except Exception as e:
            # opaque 401 error
            raise InvalidTokenError(f"{e.__class__.__name__}: {e}") from e


class Auth:
    def __init__(self, verifier: TokenVerifier) -> None:
        self._verifier = verifier

    @staticmethod
    def _bearer_token(request: Request) -> str:
        authorization = request.headers.get("authorization")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
        return authorization.split(" ", 1)[1].strip()

    async def current_principal(self, request: Request) -> Principal:
        token = self._bearer_token(request)
        try:
            claims = self._verifier.verify(token)
        except InvalidTokenError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired token")
        return Principal.from_claims(claims)

"""Google Business Profile write adapter — approved GBP work goes live here.

API surface (verified 2026-08-30): local posts and review replies remain on
the v4 API; the Q&A API was DISCONTINUED 2025-11-03 with no replacement, so
Q&A is not a feature. Note: GBP API access itself requires Google's approval
of an access request for the OAuth project.

Auth: same OAuth refresh-token flow as the read adapter. Encrypted
credentials shape on the connection row (provider "gbp"):
{"refresh_token": ..., "account_id": ..., "location_id": ...}.

Review replies are a PUT (replace) — naturally idempotent. Post creation is
NOT: the caller owns retry hygiene (see gbp_apply's docstring)."""

import httpx

from adapters.google_adapter import fetch_google_access_token
from objects.connection import Connection
from utils.token_crypto import decrypt_credentials

GBP_API_BASE = "https://mybusiness.googleapis.com/v4"
WRITE_TIMEOUT_SECONDS = 20.0


class GbpWriteError(Exception):
    """A GBP write failed. Propagates out of gbp_apply so procrastinate
    retries; exhausted retries dead-letter to the operator."""


class GBPAdapter:
    def __init__(self, connection: Connection) -> None:
        credentials = decrypt_credentials(connection.credentials_encrypted or "")
        self.refresh_token = credentials["refresh_token"]
        self.location_path = f"accounts/{credentials['account_id']}/locations/{credentials['location_id']}"

    async def create_post(self, summary: str) -> str:
        """Publish one local post; returns the post's resource name."""
        created = await self._request("POST", f"/{self.location_path}/localPosts", {"languageCode": "en-US", "topicType": "STANDARD", "summary": summary})
        return created.get("name", "")

    async def reply_to_review(self, review_external_id: str, comment: str) -> None:
        """Publish the reply to one review. PUT semantics: replaces any
        existing reply, so a retry is safe."""
        await self._request("PUT", f"/{self.location_path}/reviews/{review_external_id}/reply", {"comment": comment})

    async def _request(self, method: str, path: str, json_body: dict) -> dict:
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
            access_token = await fetch_google_access_token(client, self.refresh_token)
            try:
                response = await client.request(method, f"{GBP_API_BASE}{path}", json=json_body, headers={"Authorization": f"Bearer {access_token}"})
            except httpx.HTTPError as error:
                raise GbpWriteError(f"gbp {method} {path} failed: {error}") from error
        if response.status_code >= 400:
            raise GbpWriteError(f"gbp {method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

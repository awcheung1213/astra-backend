"""WordPress write adapter — the change-set engine's WordPress arm.

Auth: REST API application password (basic auth). Encrypted credentials
shape on the site row: {"base_url": ..., "username": ..., "app_password": ...}.

Field-mapping honesty:
- title and h1 both write the post/page TITLE (themes render it as the h1).
- meta_description writes the EXCERPT — core WordPress has no meta
  description field; most SEO plugins fall back to the excerpt.
  TODO: write the installed SEO plugin's own field (e.g. Yoast) once plugin
  detection exists.
- schema_markup appends an ld+json script block to the content; the
  rollback record is the whole previous content.

Contract with the engine: every write READS the current value first and
returns it as previous_value (reverts are built from it); anything created
returns created_ref."""

import json

import httpx

from adapters.cms.common import WRITE_TIMEOUT_SECONDS, CmsWriteError, markdown_to_html, path_slug
from objects.site import Site
from utils.token_crypto import decrypt_credentials


class WordPressAdapter:
    def __init__(self, site: Site) -> None:
        credentials = decrypt_credentials(site.credentials_encrypted or "")
        self.base_url = credentials["base_url"].rstrip("/")
        self.auth = (credentials["username"], credentials["app_password"])

    async def verify(self) -> None:
        """Cheap authed read proving base_url + credentials actually work.
        context=edit forces authentication — a public read would pass with a
        wrong password. Raises CmsWriteError on any failure."""
        async with httpx.AsyncClient(auth=self.auth, timeout=WRITE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            await self._request(client, "GET", "/wp-json/wp/v2/pages?per_page=1&context=edit")

    async def apply(self, operation: dict) -> dict:
        async with httpx.AsyncClient(auth=self.auth, timeout=WRITE_TIMEOUT_SECONDS, follow_redirects=True) as client:
            if operation["op"] == "publish_page":
                return await self._publish_page(client, operation["value"])
            if operation["op"] == "unpublish_page":
                return await self._unpublish_page(client, operation["target"])

            kind, page = await self._page_for_url(client, operation["target"])
            if operation["op"] == "update_page":
                return await self._update_page(client, kind, page, operation["value"])
            if operation["op"] == "set_field":
                return await self._set_field(client, kind, page, operation["field"], operation["value"])
            raise CmsWriteError(f"wordpress adapter does not know operation {operation['op']!r}")

    ##############################
    # OPERATIONS
    ##############################

    async def _set_field(self, client: httpx.AsyncClient, kind: str, page: dict, field: str, value) -> dict:
        if field in ("title", "h1"):
            previous_value = _raw(page.get("title"))
            await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {"title": value})
            return {"previous_value": previous_value}
        if field == "meta_description":
            previous_value = _raw(page.get("excerpt"))
            await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {"excerpt": value})
            return {"previous_value": previous_value}
        if field == "schema_markup":
            previous_content = _raw(page.get("content"))
            content_with_schema = f'{previous_content}\n<script type="application/ld+json">{json.dumps(value)}</script>'
            await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {"content": content_with_schema})
            return {"previous_value": previous_content}  # rollback restores the whole content
        raise CmsWriteError(f"wordpress adapter does not know field {field!r}")

    async def _update_page(self, client: httpx.AsyncClient, kind: str, page: dict, value: dict) -> dict:
        previous_value = {"title": _raw(page.get("title")), "meta_description": _raw(page.get("excerpt")), "body_markdown": _raw(page.get("content"))}
        await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {"title": value["title"], "excerpt": value["meta_description"], "content": markdown_to_html(value["body_markdown"])})
        return {"previous_value": previous_value}

    async def _publish_page(self, client: httpx.AsyncClient, value: dict) -> dict:
        created = await self._request(client, "POST", "/wp-json/wp/v2/pages", {"title": value["title"], "excerpt": value["meta_description"], "content": markdown_to_html(value["body_markdown"]), "status": "publish"})
        return {"previous_value": None, "created_ref": str(created["id"])}

    async def _unpublish_page(self, client: httpx.AsyncClient, created_ref: str) -> dict:
        page = await self._request(client, "GET", f"/wp-json/wp/v2/pages/{created_ref}?context=edit")
        await self._request(client, "POST", f"/wp-json/wp/v2/pages/{created_ref}", {"status": "draft"})
        return {"previous_value": page.get("status")}

    ##############################
    # PLUMBING
    ##############################

    async def _page_for_url(self, client: httpx.AsyncClient, url: str) -> tuple[str, dict]:
        """Resolve a live URL to its WP object: pages first, posts as fallback.
        context=edit returns RAW field values — what reads/writes round-trip on."""
        slug = path_slug(url)
        for kind in ("pages", "posts"):
            matches = await self._request(client, "GET", f"/wp-json/wp/v2/{kind}?slug={slug}&context=edit")
            if matches:
                return kind, matches[0]
        raise CmsWriteError(f"no wordpress page or post with slug {slug!r} (from {url})")

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, json_body: dict | None = None):
        try:
            response = await client.request(method, f"{self.base_url}{path}", json=json_body)
        except httpx.HTTPError as error:
            raise CmsWriteError(f"wordpress {method} {path} failed: {error}") from error
        if response.status_code >= 400:
            raise CmsWriteError(f"wordpress {method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json()


def _raw(field_payload: dict | None) -> str:
    """WP rich fields come as {'raw': ..., 'rendered': ...} under context=edit."""
    return (field_payload or {}).get("raw") or ""

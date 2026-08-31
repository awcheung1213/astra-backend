"""Shopify write adapter — the change-set engine's Shopify arm.

Auth: Admin API access token. Encrypted credentials shape on the site row:
{"shop_domain": "example.myshopify.com", "access_token": "shpat_..."}.

Field-mapping honesty:
- title and h1 both write the page TITLE (themes render it as the h1).
- meta_description writes the page's global.description_tag METAFIELD —
  Shopify's real meta description slot.
- schema_markup appends an ld+json script block to body_html; the rollback
  record is the whole previous body_html.

Contract with the engine: every write READS the current value first and
returns it as previous_value; anything created returns created_ref."""

import json

import httpx

from adapters.cms.common import WRITE_TIMEOUT_SECONDS, CmsWriteError, markdown_to_html, path_slug
from objects.site import Site
from utils.token_crypto import decrypt_credentials

API_VERSION = "2024-07"


class ShopifyAdapter:
    def __init__(self, site: Site) -> None:
        credentials = decrypt_credentials(site.credentials_encrypted or "")
        self.base_url = f"https://{credentials['shop_domain']}/admin/api/{API_VERSION}"
        self.headers = {"X-Shopify-Access-Token": credentials["access_token"]}

    async def apply(self, operation: dict) -> dict:
        async with httpx.AsyncClient(headers=self.headers, timeout=WRITE_TIMEOUT_SECONDS) as client:
            if operation["op"] == "publish_page":
                return await self._publish_page(client, operation["value"])
            if operation["op"] == "unpublish_page":
                return await self._unpublish_page(client, operation["target"])

            page = await self._page_for_url(client, operation["target"])
            if operation["op"] == "update_page":
                return await self._update_page(client, page, operation["value"])
            if operation["op"] == "set_field":
                return await self._set_field(client, page, operation["field"], operation["value"])
            raise CmsWriteError(f"shopify adapter does not know operation {operation['op']!r}")

    ##############################
    # OPERATIONS
    ##############################

    async def _set_field(self, client: httpx.AsyncClient, page: dict, field: str, value) -> dict:
        if field in ("title", "h1"):
            previous_value = page.get("title")
            await self._request(client, "PUT", f"/pages/{page['id']}.json", {"page": {"id": page["id"], "title": value}})
            return {"previous_value": previous_value}
        if field == "meta_description":
            return await self._set_description_metafield(client, page, value)
        if field == "schema_markup":
            previous_body = page.get("body_html") or ""
            body_with_schema = f'{previous_body}\n<script type="application/ld+json">{json.dumps(value)}</script>'
            await self._request(client, "PUT", f"/pages/{page['id']}.json", {"page": {"id": page["id"], "body_html": body_with_schema}})
            return {"previous_value": previous_body}  # rollback restores the whole body
        raise CmsWriteError(f"shopify adapter does not know field {field!r}")

    async def _update_page(self, client: httpx.AsyncClient, page: dict, value: dict) -> dict:
        description_result = await self._set_description_metafield(client, page, value["meta_description"])
        previous_value = {"title": page.get("title"), "meta_description": description_result["previous_value"], "body_markdown": page.get("body_html") or ""}
        await self._request(client, "PUT", f"/pages/{page['id']}.json", {"page": {"id": page["id"], "title": value["title"], "body_html": markdown_to_html(value["body_markdown"])}})
        return {"previous_value": previous_value}

    async def _publish_page(self, client: httpx.AsyncClient, value: dict) -> dict:
        created = await self._request(client, "POST", "/pages.json", {"page": {"title": value["title"], "body_html": markdown_to_html(value["body_markdown"]), "published": True}})
        created_page = created["page"]
        await self._set_description_metafield(client, created_page, value["meta_description"])
        return {"previous_value": None, "created_ref": str(created_page["id"])}

    async def _unpublish_page(self, client: httpx.AsyncClient, created_ref: str) -> dict:
        await self._request(client, "PUT", f"/pages/{created_ref}.json", {"page": {"id": int(created_ref), "published": False}})
        return {"previous_value": "published"}

    ##############################
    # PLUMBING
    ##############################

    async def _page_for_url(self, client: httpx.AsyncClient, url: str) -> dict:
        handle = path_slug(url)
        found = await self._request(client, "GET", f"/pages.json?handle={handle}")
        pages = found.get("pages") or []
        if not pages:
            raise CmsWriteError(f"no shopify page with handle {handle!r} (from {url})")
        return pages[0]

    async def _set_description_metafield(self, client: httpx.AsyncClient, page: dict, value: str) -> dict:
        """global.description_tag is Shopify's meta-description slot; read the
        existing metafield first so the previous value survives for revert."""
        existing = await self._request(client, "GET", f"/pages/{page['id']}/metafields.json?namespace=global&key=description_tag")
        metafields = existing.get("metafields") or []
        if metafields:
            previous_value = metafields[0].get("value")
            await self._request(client, "PUT", f"/metafields/{metafields[0]['id']}.json", {"metafield": {"id": metafields[0]["id"], "value": value, "type": "single_line_text_field"}})
        else:
            previous_value = None
            await self._request(client, "POST", f"/pages/{page['id']}/metafields.json", {"metafield": {"namespace": "global", "key": "description_tag", "value": value, "type": "single_line_text_field"}})
        return {"previous_value": previous_value}

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, json_body: dict | None = None):
        try:
            response = await client.request(method, f"{self.base_url}{path}", json=json_body)
        except httpx.HTTPError as error:
            raise CmsWriteError(f"shopify {method} {path} failed: {error}") from error
        if response.status_code >= 400:
            raise CmsWriteError(f"shopify {method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json()

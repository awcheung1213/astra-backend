"""Shopify write adapter — the change-set engine's Shopify arm.

Auth: client credentials grant (the 2026+ custom-app model — Dev Dashboard
apps expose a client id/secret, never a visible admin token; tokens are
minted per adapter instance and live 24h). Encrypted credentials shape on
the site row: {"shop_domain": "example.myshopify.com", "client_id": ...,
"client_secret": ...}. The app must be installed on the store, belong to the
same Shopify organization, and its released version must carry the
read_content/write_content scopes (Pages).

Fields are the CMS's NATIVE ones — `title`, `body_html`, `meta_description`
(the global.description_tag METAFIELD, Shopify's real meta description slot)
— with aliases `h1` -> title (themes render the title as the H1) and
`content` -> body_html. Composite changes (a JSON-LD block) are a body_html
write the agent composes after READING the current body — no per-SEO-feature
branches (agent-tools-plan.md Phase 1).

Contract with the engine: every write READS the current value of the SAME
field first and returns it as previous_value; anything created returns
created_ref. Also carries the Technical agent's read primitives."""

import httpx

from adapters.cms.common import WRITE_TIMEOUT_SECONDS, CmsWriteError, markdown_to_html, path_slug
from objects.site import Site
from utils.token_crypto import decrypt_credentials

API_VERSION = "2024-07"


class ShopifyAdapter:
    NATIVE_FIELDS = ("title", "body_html", "meta_description")
    FIELD_ALIASES = {"h1": "title", "content": "body_html"}

    @classmethod
    def resolve_field(cls, field: str) -> str:
        """The native field a proposal's field name writes; CmsWriteError for
        anything this CMS has no slot for."""
        native = cls.FIELD_ALIASES.get(field, field)
        if native not in cls.NATIVE_FIELDS:
            raise CmsWriteError(f"shopify has no writable field {field!r} (native: {', '.join(cls.NATIVE_FIELDS)})")
        return native

    def __init__(self, site: Site) -> None:
        credentials = decrypt_credentials(site.credentials_encrypted or "")
        self.shop_domain = credentials["shop_domain"]
        self.base_url = f"https://{self.shop_domain}/admin/api/{API_VERSION}"
        self.client_id = credentials["client_id"]
        self.client_secret = credentials["client_secret"]
        self._access_token: str | None = None

    async def verify(self) -> None:
        """Cheap authed read proving the whole chain works: shop_domain +
        client id/secret mint a token (wrong secret or an uninstalled app
        fails HERE with Shopify's error), and the token can read the shop.
        Raises CmsWriteError on any failure."""
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
            await self._request(client, "GET", "/shop.json")

    async def apply(self, operation: dict) -> dict:
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
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
    # READS (the agent's eyes — never write)
    ##############################

    async def get_page(self, url: str) -> dict:
        """The store page behind a live URL with its native field values."""
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
            page = await self._page_for_url(client, url)
            description = await self._description_metafield(client, page)
        return {"id": page.get("id"), "url": self._page_url(page), "handle": page.get("handle"), "title": page.get("title"), "body_html": page.get("body_html") or "", "meta_description": (description or {}).get("value")}

    async def get_settings(self) -> dict:
        """Store-level facts: name and domains."""
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
            shop = (await self._request(client, "GET", "/shop.json")).get("shop") or {}
        return {"name": shop.get("name"), "domain": shop.get("domain"), "myshopify_domain": shop.get("myshopify_domain")}

    async def list_pages(self) -> list[dict]:
        """Store pages (id, url, handle, title) — no body, so it stays small."""
        async with httpx.AsyncClient(timeout=WRITE_TIMEOUT_SECONDS) as client:
            pages = (await self._request(client, "GET", "/pages.json?limit=250&fields=id,handle,title")).get("pages") or []
        return [{"id": page.get("id"), "url": self._page_url(page), "handle": page.get("handle"), "title": page.get("title")} for page in pages]

    def _page_url(self, page: dict) -> str:
        return f"https://{self.shop_domain}/pages/{page.get('handle', '')}"

    ##############################
    # OPERATIONS
    ##############################

    async def _set_field(self, client: httpx.AsyncClient, page: dict, field: str, value) -> dict:
        native = self.resolve_field(field)
        if native == "meta_description":
            return await self._set_description_metafield(client, page, value)
        previous_value = page.get(native) or ""
        await self._request(client, "PUT", f"/pages/{page['id']}.json", {"page": {"id": page["id"], native: value}})
        return {"previous_value": previous_value}

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

    async def _description_metafield(self, client: httpx.AsyncClient, page: dict) -> dict | None:
        """The page's global.description_tag metafield row, if any."""
        existing = await self._request(client, "GET", f"/pages/{page['id']}/metafields.json?namespace=global&key=description_tag")
        metafields = existing.get("metafields") or []
        return metafields[0] if metafields else None

    async def _set_description_metafield(self, client: httpx.AsyncClient, page: dict, value: str) -> dict:
        """global.description_tag is Shopify's meta-description slot; read the
        existing metafield first so the previous value survives for revert."""
        metafield = await self._description_metafield(client, page)
        if metafield:
            previous_value = metafield.get("value")
            await self._request(client, "PUT", f"/metafields/{metafield['id']}.json", {"metafield": {"id": metafield["id"], "value": value, "type": "single_line_text_field"}})
        else:
            previous_value = None
            await self._request(client, "POST", f"/pages/{page['id']}/metafields.json", {"metafield": {"namespace": "global", "key": "description_tag", "value": value, "type": "single_line_text_field"}})
        return {"previous_value": previous_value}

    async def _fetch_access_token(self, client: httpx.AsyncClient) -> str:
        """Client-credentials grant: id/secret -> a 24h Admin API token,
        minted once per adapter instance (instances live one task run)."""
        if self._access_token is not None:
            return self._access_token
        try:
            response = await client.post(f"https://{self.shop_domain}/admin/oauth/access_token", data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret})
        except httpx.HTTPError as error:
            raise CmsWriteError(f"shopify token exchange failed: {error}") from error
        if response.status_code >= 400:
            raise CmsWriteError(f"shopify token exchange -> {response.status_code}: {response.text[:200]} (is the app installed on this store, in the same organization?)")
        self._access_token = response.json()["access_token"]
        return self._access_token

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, json_body: dict | None = None):
        access_token = await self._fetch_access_token(client)
        try:
            response = await client.request(method, f"{self.base_url}{path}", json=json_body, headers={"X-Shopify-Access-Token": access_token})
        except httpx.HTTPError as error:
            raise CmsWriteError(f"shopify {method} {path} failed: {error}") from error
        if response.status_code >= 400:
            raise CmsWriteError(f"shopify {method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json()

"""WordPress adapter — the change-set engine's WordPress arm, plus the
Technical agent's read primitives (get_page / get_settings / list_pages).

Auth: REST API application password (basic auth). Encrypted credentials
shape on the site row: {"base_url": ..., "username": ..., "app_password": ...}.

Fields are the CMS's NATIVE ones — `title`, `excerpt`, `content` — with two
aliases kept for older proposals: `h1` -> title (themes render the page title
as the H1) and `meta_description` -> excerpt (core WordPress has no meta
description field; most SEO plugins fall back to the excerpt). Anything
composite (a JSON-LD block, a fixed link) is a `content` write the agent
composes after READING the current content — there are no per-SEO-feature
branches here on purpose (agent-tools-plan.md Phase 1).

Google descriptions: with Yoast SEO active, Google reads YOAST's description
(`_yoast_wpseo_metadesc`), not the excerpt — so a meta_description write goes
to that meta key. Yoast doesn't expose the key over REST; the free
SEO_BRIDGE_PLUGIN from the wordpress.org directory does. When the key isn't
writable, the write raises SeoBridgeNeeded BEFORE touching anything and the
engine asks the owner to approve `install_plugin` (undo = deactivate).

Contract with the engine: every write READS the current value of the SAME
field first, checkpoints it BEFORE writing, and returns it as previous_value
(reverts restore it); anything created checkpoints + returns created_ref, and
a publish whose op already carries a created_ref never creates again."""

from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.session import HttpMethod

from adapters.cms.common import WRITE_TIMEOUT_SECONDS, Checkpoint, CmsWriteError, SeoBridgeNeeded, markdown_to_html, no_checkpoint, path_slug
from objects.site import Site
from utils.site_fetch import RequestsError, site_session
from utils.token_crypto import decrypt_credentials

SEO_BRIDGE_PLUGIN = "seo-fields-api-support"  # wordpress.org slug: registers Yoast's title/description meta for REST writes
YOAST_DESCRIPTION_META = "_yoast_wpseo_metadesc"
SEO_YOAST = "yoast"
# a description fix names either field (proposals use the alias; agent-mode fixes the native slot) — on a Yoast site both mean Yoast's description
DESCRIPTION_WRITE_FIELDS = ("meta_description", "excerpt")


def seo_plugin_from_namespaces(namespaces: list[str]) -> str | None:
    """The SEO plugin a site runs, from its REST index namespaces. Pure."""
    return SEO_YOAST if "yoast/v1" in namespaces else None


class WordPressAdapter:
    NATIVE_FIELDS = ("title", "excerpt", "content")
    FIELD_ALIASES = {"h1": "title", "meta_description": "excerpt"}

    def __init__(self, site: Site) -> None:
        credentials = decrypt_credentials(site.credentials_encrypted or "")
        self.base_url = credentials["base_url"].rstrip("/")
        self.auth = (credentials["username"], credentials["app_password"])

    @classmethod
    def resolve_field(cls, field: str) -> str:
        """The native field a proposal's field name writes; CmsWriteError for
        anything this CMS has no slot for."""
        native = cls.FIELD_ALIASES.get(field, field)
        if native not in cls.NATIVE_FIELDS:
            raise CmsWriteError(f"wordpress has no writable field {field!r} (native: {', '.join(cls.NATIVE_FIELDS)})")
        return native

    async def verify(self) -> None:
        """Cheap authed read proving base_url + credentials actually work.
        context=edit forces authentication — a public read would pass with a
        wrong password. Raises CmsWriteError on any failure."""
        async with site_session(timeout=WRITE_TIMEOUT_SECONDS, auth=self.auth) as client:
            await self._request(client, "GET", "/wp-json/wp/v2/pages?per_page=1&context=edit")

    async def apply(self, operation: dict, checkpoint: Checkpoint = no_checkpoint) -> dict:
        async with site_session(timeout=WRITE_TIMEOUT_SECONDS, auth=self.auth) as client:
            if operation["op"] == "publish_page":
                return await self._publish_page(client, operation["value"], operation.get("created_ref"), checkpoint)
            if operation["op"] == "unpublish_page":
                return await self._unpublish_page(client, operation["target"], checkpoint)
            if operation["op"] == "install_plugin":
                return await self._install_plugin(client, operation["value"]["slug"], checkpoint)
            if operation["op"] == "deactivate_plugin":
                return await self._set_plugin_status(client, operation["target"], "inactive", checkpoint)

            kind, page = await self._page_for_url(client, operation["target"])
            if operation["op"] == "update_page":
                return await self._update_page(client, kind, page, operation["value"], checkpoint)
            if operation["op"] == "set_field":
                return await self._set_field(client, kind, page, operation["field"], operation["value"], checkpoint)
            raise CmsWriteError(f"wordpress adapter does not know operation {operation['op']!r}")

    ##############################
    # READS (the agent's eyes — never write)
    ##############################

    async def get_page(self, url: str) -> dict:
        """The page behind a live URL with its RAW native field values."""
        async with site_session(timeout=WRITE_TIMEOUT_SECONDS, auth=self.auth) as client:
            kind, page = await self._page_for_url(client, url)
        return _page_summary(kind, page, include_content=True)

    async def get_settings(self) -> dict:
        """Site-level facts: name, tagline, front-page mode, base URL."""
        async with site_session(timeout=WRITE_TIMEOUT_SECONDS, auth=self.auth) as client:
            settings = await self._request(client, "GET", "/wp-json/wp/v2/settings")
        return {"title": settings.get("title"), "description": settings.get("description"), "url": settings.get("url"), "show_on_front": settings.get("show_on_front"), "page_on_front": settings.get("page_on_front")}

    async def list_pages(self) -> list[dict]:
        """Published pages (id, url, slug, title) — no content, so it stays small."""
        async with site_session(timeout=WRITE_TIMEOUT_SECONDS, auth=self.auth) as client:
            pages = await self._request(client, "GET", "/wp-json/wp/v2/pages?per_page=100&status=publish&context=edit")
        return [_page_summary("pages", page, include_content=False) for page in pages]

    ##############################
    # OPERATIONS
    ##############################

    async def _set_field(self, client: AsyncSession, kind: str, page: dict, field: str, value, checkpoint: Checkpoint = no_checkpoint) -> dict:
        if field in DESCRIPTION_WRITE_FIELDS and await self._seo_plugin(client) == SEO_YOAST:
            return await self._set_yoast_description(client, kind, page, value, checkpoint)
        native = self.resolve_field(field)
        previous_value = _raw(page.get(native))
        await checkpoint({"previous_value": previous_value})
        await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {native: value})
        return {"previous_value": previous_value}

    async def _set_yoast_description(self, client: AsyncSession, kind: str, page: dict, value, checkpoint: Checkpoint = no_checkpoint) -> dict:
        """Yoast's own description field. context=edit lists only REST-registered
        meta, so a missing key = the bridge isn't installed: raise before any
        write (nothing to roll back)."""
        raw_meta = page.get("meta")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        if YOAST_DESCRIPTION_META not in meta:
            raise SeoBridgeNeeded(f"yoast description isn't writable over REST on {self.base_url} (install {SEO_BRIDGE_PLUGIN})")
        previous_value = meta.get(YOAST_DESCRIPTION_META) or ""
        await checkpoint({"previous_value": previous_value})
        await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {"meta": {YOAST_DESCRIPTION_META: value}})
        return {"previous_value": previous_value}

    async def _install_plugin(self, client: AsyncSession, slug: str, checkpoint: Checkpoint = no_checkpoint) -> dict:
        """Install + activate a wordpress.org plugin (REST /wp/v2/plugins).
        Already installed -> just activate it. created_ref = the plugin's
        "dir/file" id (what the undo deactivates); previous_value = its prior
        status (None when it wasn't installed)."""
        existing = await self._plugin_by_slug(client, slug)
        if existing is not None:
            await checkpoint({"previous_value": existing.get("status"), "created_ref": existing["plugin"]})
            if existing.get("status") != "active":
                await self._request(client, "POST", f"/wp-json/wp/v2/plugins/{existing['plugin']}", {"status": "active"})
            return {"previous_value": existing.get("status"), "created_ref": existing["plugin"]}
        await checkpoint({"previous_value": None})
        installed = await self._request(client, "POST", "/wp-json/wp/v2/plugins", {"slug": slug, "status": "active"})
        await checkpoint({"created_ref": installed["plugin"]})
        return {"previous_value": None, "created_ref": installed["plugin"]}

    async def _set_plugin_status(self, client: AsyncSession, plugin: str, status: str, checkpoint: Checkpoint = no_checkpoint) -> dict:
        current = await self._request(client, "GET", f"/wp-json/wp/v2/plugins/{plugin}")
        await checkpoint({"previous_value": current.get("status")})
        await self._request(client, "POST", f"/wp-json/wp/v2/plugins/{plugin}", {"status": status})
        return {"previous_value": current.get("status")}

    async def _plugin_by_slug(self, client: AsyncSession, slug: str) -> dict | None:
        plugins = await self._request(client, "GET", f"/wp-json/wp/v2/plugins?search={slug}")
        return next((plugin for plugin in plugins if str(plugin.get("plugin", "")).split("/", 1)[0] == slug), None)

    async def _seo_plugin(self, client: AsyncSession) -> str | None:
        if not hasattr(self, "_seo_plugin_name"):
            index = await self._request(client, "GET", "/wp-json/")
            self._seo_plugin_name = seo_plugin_from_namespaces(index.get("namespaces") or [])
        return self._seo_plugin_name

    async def _update_page(self, client: AsyncSession, kind: str, page: dict, value: dict, checkpoint: Checkpoint = no_checkpoint) -> dict:
        previous_value = {"title": _raw(page.get("title")), "meta_description": _raw(page.get("excerpt")), "body_markdown": _raw(page.get("content"))}
        await checkpoint({"previous_value": previous_value})
        await self._request(client, "POST", f"/wp-json/wp/v2/{kind}/{page['id']}", {"title": value["title"], "excerpt": value["meta_description"], "content": markdown_to_html(value["body_markdown"])})
        return {"previous_value": previous_value}

    async def _publish_page(self, client: AsyncSession, value: dict, created_ref: str | None = None, checkpoint: Checkpoint = no_checkpoint) -> dict:
        if created_ref:
            return {"previous_value": None, "created_ref": created_ref}  # a retry: the page already exists — never publish a duplicate
        created = await self._request(client, "POST", "/wp-json/wp/v2/pages", {"title": value["title"], "excerpt": value["meta_description"], "content": markdown_to_html(value["body_markdown"]), "status": "publish"})
        await checkpoint({"created_ref": str(created["id"])})
        return {"previous_value": None, "created_ref": str(created["id"])}

    async def _unpublish_page(self, client: AsyncSession, created_ref: str, checkpoint: Checkpoint = no_checkpoint) -> dict:
        page = await self._request(client, "GET", f"/wp-json/wp/v2/pages/{created_ref}?context=edit")
        await checkpoint({"previous_value": page.get("status")})
        await self._request(client, "POST", f"/wp-json/wp/v2/pages/{created_ref}", {"status": "draft"})
        return {"previous_value": page.get("status")}

    ##############################
    # PLUMBING
    ##############################

    async def _page_for_url(self, client: AsyncSession, url: str) -> tuple[str, dict]:
        """Resolve a live URL to its WP object: the homepage via the site's
        front-page setting, anything else by slug — pages first, posts as
        fallback. context=edit returns RAW field values — what reads/writes
        round-trip on."""
        if not urlparse(url).path.strip("/"):
            return "pages", await self._front_page(client)
        slug = path_slug(url)
        for kind in ("pages", "posts"):
            matches = await self._request(client, "GET", f"/wp-json/wp/v2/{kind}?slug={slug}&context=edit")
            if matches:
                return kind, matches[0]
        raise CmsWriteError(f"no wordpress page or post with slug {slug!r} (from {url})")

    async def _front_page(self, client: AsyncSession) -> dict:
        """The homepage has no slug: WordPress's Reading setting says whether it
        is a static page (page_on_front) or the posts index — the latter has no
        page object to write to, so that is a clear error, not a guess."""
        settings = await self._request(client, "GET", "/wp-json/wp/v2/settings")
        if settings.get("show_on_front") != "page" or not settings.get("page_on_front"):
            raise CmsWriteError("the homepage is the posts index, not a page — nothing to write to (set a static front page in Settings > Reading)")
        return await self._request(client, "GET", f"/wp-json/wp/v2/pages/{settings['page_on_front']}?context=edit")

    async def _request(self, client: AsyncSession, method: HttpMethod, path: str, json_body: dict | None = None):
        try:
            response = await client.request(method, f"{self.base_url}{path}", json=json_body)
        except RequestsError as error:
            raise CmsWriteError(f"wordpress {method} {path} failed: {error}") from error
        if response.status_code >= 400:
            raise CmsWriteError(f"wordpress {method} {path} -> {response.status_code}: {response.text[:200]}", status_code=response.status_code)
        return response.json()


def _raw(field_payload: dict | None) -> str:
    """WP rich fields come as {'raw': ..., 'rendered': ...} under context=edit."""
    return (field_payload or {}).get("raw") or ""


def _page_summary(kind: str, page: dict, include_content: bool) -> dict:
    summary = {"id": page.get("id"), "kind": kind, "url": page.get("link"), "slug": page.get("slug"), "status": page.get("status"), "title": _raw(page.get("title")), "excerpt": _raw(page.get("excerpt"))}
    if include_content:
        summary["content"] = _raw(page.get("content"))
    return summary

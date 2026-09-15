"""WordPress adapter URL resolution — no site, no network: _request is stubbed."""

import pytest

from adapters.cms.common import CmsWriteError
from adapters.cms.shopify_adapter import ShopifyAdapter
from adapters.cms.wordpress_adapter import WordPressAdapter
from worker.tasks.changes import native_field


def _adapter(responses: dict[str, object]) -> WordPressAdapter:
    adapter = WordPressAdapter.__new__(WordPressAdapter)
    adapter.base_url = "https://example.com"
    adapter.auth = ("u", "p")
    adapter.calls = []  # type: ignore[attr-defined]

    async def fake_request(client, method, path, json_body=None):
        adapter.calls.append((method, path))  # type: ignore[attr-defined]
        return responses[path]

    adapter._request = fake_request  # type: ignore[method-assign]
    return adapter


async def test_homepage_resolves_through_the_front_page_setting():
    adapter = _adapter({"/wp-json/wp/v2/settings": {"show_on_front": "page", "page_on_front": 7}, "/wp-json/wp/v2/pages/7?context=edit": {"id": 7, "title": {"raw": "Home"}}})
    for homepage in ("https://example.com", "https://example.com/"):
        kind, page = await adapter._page_for_url(None, homepage)
        assert (kind, page["id"]) == ("pages", 7)


async def test_homepage_that_is_the_posts_index_is_a_clear_error():
    adapter = _adapter({"/wp-json/wp/v2/settings": {"show_on_front": "posts", "page_on_front": 0}})
    with pytest.raises(CmsWriteError, match="posts index"):
        await adapter._page_for_url(None, "https://example.com/")


async def test_inner_urls_still_resolve_by_slug():
    adapter = _adapter({"/wp-json/wp/v2/pages?slug=about&context=edit": [{"id": 3}]})
    assert await adapter._page_for_url(None, "https://example.com/about/") == ("pages", {"id": 3})


def test_fields_resolve_to_the_cms_native_slot_or_nothing():
    assert WordPressAdapter.resolve_field("h1") == "title"
    assert WordPressAdapter.resolve_field("meta_description") == "excerpt"
    assert WordPressAdapter.resolve_field("content") == "content"
    assert ShopifyAdapter.resolve_field("content") == "body_html"
    assert ShopifyAdapter.resolve_field("meta_description") == "meta_description"
    with pytest.raises(CmsWriteError):
        WordPressAdapter.resolve_field("schema_markup")  # composed into content now, never a field
    assert native_field("wordpress", "h1") == "title"
    assert native_field("shopify", "excerpt") is None
    assert native_field("wix", "title") is None


async def test_set_field_reads_and_writes_the_same_native_field():
    adapter = _adapter({})
    writes = []

    async def fake_request(client, method, path, json_body=None):
        writes.append((method, path, json_body))
        return {}

    adapter._request = fake_request  # type: ignore[method-assign]
    page = {"id": 7, "title": {"raw": "Old title"}, "excerpt": {"raw": "Old excerpt"}, "content": {"raw": "<p>old</p>"}}
    assert await adapter._set_field(None, "pages", page, "meta_description", "New excerpt") == {"previous_value": "Old excerpt"}
    assert await adapter._set_field(None, "pages", page, "content", "<p>old</p><script></script>") == {"previous_value": "<p>old</p>"}
    assert writes == [("POST", "/wp-json/wp/v2/pages/7", {"excerpt": "New excerpt"}), ("POST", "/wp-json/wp/v2/pages/7", {"content": "<p>old</p><script></script>"})]


async def test_get_page_returns_the_native_summary():
    adapter = _adapter({"/wp-json/wp/v2/pages?slug=about&context=edit": [{"id": 3, "link": "https://example.com/about/", "slug": "about", "status": "publish", "title": {"raw": "About"}, "excerpt": {"raw": ""}, "content": {"raw": "<p>hi</p>"}}]})

    class _NoSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    import adapters.cms.wordpress_adapter as module

    original = module.site_session
    module.site_session = lambda **kwargs: _NoSession()  # type: ignore[assignment]
    try:
        page = await adapter.get_page("https://example.com/about/")
    finally:
        module.site_session = original  # type: ignore[assignment]
    assert page == {"id": 3, "kind": "pages", "url": "https://example.com/about/", "slug": "about", "status": "publish", "title": "About", "excerpt": "", "content": "<p>hi</p>"}

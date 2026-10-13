"""Audit rule engine — pure functions over fetched text."""

import worker.tasks.site_audit as site_audit
from worker.tasks.site_audit import (
    KIND_MISSING_H1,
    KIND_MISSING_META_DESCRIPTION,
    KIND_MISSING_SCHEMA,
    KIND_MISSING_TITLE,
    KIND_SLOW_PAGE,
    extract_internal_links,
    finding_key,
    is_sitemap_document,
    page_facts,
    page_findings,
    seo_plugin,
    robots_blocked_agents,
    robots_sitemap_urls,
    sitemap_candidate_urls,
)

GOOD_HTML = """
<html><head><title>Best Nail Salon</title>
<meta name="description" content="A very good salon.">
<script type="application/ld+json">{"@type": "LocalBusiness"}</script>
</head><body><h1>Welcome</h1></body></html>
"""


def kinds(findings):
    return {f["kind"] for f in findings}


def test_clean_page_has_no_findings():
    assert page_findings("https://x.com/", GOOD_HTML, elapsed_seconds=0.4) == []


def test_empty_page_flags_all_four_content_checks():
    found = kinds(page_findings("https://x.com/", "<html></html>", elapsed_seconds=0.4))
    assert found == {KIND_MISSING_TITLE, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_H1, KIND_MISSING_SCHEMA}


def test_slow_page_flags():
    assert KIND_SLOW_PAGE in kinds(page_findings("https://x.com/", GOOD_HTML, elapsed_seconds=5.0))


def test_meta_description_attribute_order_does_not_matter():
    html = '<html><head><title>t</title><meta content="desc here" name="description"></head><body><h1>h</h1><script type="application/ld+json">{}</script></body></html>'
    assert KIND_MISSING_META_DESCRIPTION not in kinds(page_findings("https://x.com/", html, 0.1))


def test_robots_blocks_named_agent():
    robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin\n"
    assert robots_blocked_agents(robots) == ["GPTBot"]


def test_robots_wildcard_blocks_everyone_without_own_section():
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: ClaudeBot\nDisallow: /private\n"
    blocked = robots_blocked_agents(robots)
    assert "ClaudeBot" not in blocked  # has its own, permissive section
    assert "GPTBot" in blocked  # falls back to the blocking wildcard


def test_robots_open_site_blocks_nobody():
    assert robots_blocked_agents("User-agent: *\nDisallow:\n") == []


def test_extract_internal_links_filters_and_dedupes():
    html = '<a href="/about">a</a> <a href="https://x.com/pricing#plans">b</a> <a href="https://other.com/">c</a> <a href="/about">d</a> <a href="mailto:hi@x.com">e</a>'
    assert extract_internal_links("https://x.com/", html) == ["https://x.com/about", "https://x.com/pricing"]


def test_finding_key_is_kind_plus_url():
    assert finding_key("missing_title", {"url": "https://x.com/a"}) == ("missing_title", "https://x.com/a")
    assert finding_key("missing_sitemap", {}) == ("missing_sitemap", "")


def test_robots_sitemap_directives_are_collected_in_order():
    robots = "User-agent: *\nDisallow:\nSitemap: https://x.com/sitemap_index.xml\n# Sitemap: https://x.com/ignored.xml\nsitemap: https://x.com/news.xml\nSitemap: https://x.com/sitemap_index.xml\nSitemap: /relative.xml\n"
    assert robots_sitemap_urls(robots) == ["https://x.com/sitemap_index.xml", "https://x.com/news.xml"]


def test_sitemap_candidates_prefer_robots_then_common_paths():
    robots = "Sitemap: https://x.com/wp-sitemap.xml\n"
    assert sitemap_candidate_urls("https://x.com/", robots) == ["https://x.com/wp-sitemap.xml", "https://x.com/sitemap.xml", "https://x.com/sitemap_index.xml"]
    assert sitemap_candidate_urls("https://x.com/", None) == ["https://x.com/sitemap.xml", "https://x.com/sitemap_index.xml", "https://x.com/wp-sitemap.xml"]


def test_sitemap_document_requires_sitemap_xml_not_just_a_200():
    assert is_sitemap_document("application/xml", '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://x.com/</loc></url></urlset>')
    assert is_sitemap_document("text/xml; charset=UTF-8", "<sitemapindex><sitemap><loc>https://x.com/a.xml</loc></sitemap></sitemapindex>")
    assert not is_sitemap_document("text/html; charset=UTF-8", "<html><body>Not found</body></html>")  # soft-404
    assert not is_sitemap_document("application/xml", "<rss><channel></channel></rss>")


def test_page_facts_capture_what_the_crawler_saw():
    html = '<html><head><title> Best <b>Nail</b> Salon </title><meta content="A very good salon." name="description"></head><body><h1>Welcome</h1><h1></h1><script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization"},{"@type":["WebSite","Thing"]}]}</script><script type="application/ld+json">not json</script><p>Hello there world</p></body></html>'
    facts = page_facts(html, elapsed_seconds=1.234)
    assert facts == {"title": "Best Nail Salon", "meta_description": "A very good salon.", "h1s": ["Welcome"], "ld_json_types": ["Organization", "WebSite", "Thing"], "word_count": 7, "seo_plugin": None, "elapsed_seconds": 1.23}


def test_findings_carry_the_page_facts():
    (finding,) = page_findings("https://x.com/", '<html><head><title>t</title><meta name="description" content="d"></head><body><h1>h</h1></body></html>', 0.1)
    assert finding["kind"] == KIND_MISSING_SCHEMA
    assert finding["detail"]["facts"]["title"] == "t" and finding["detail"]["facts"]["ld_json_types"] == []


def test_seo_plugin_detected_from_head_markers():
    assert seo_plugin("<html><head><!-- This site is optimized with the Yoast SEO plugin v22 --><title>t</title></head></html>") == "yoast"
    assert seo_plugin('<html><head><meta name="generator" content="Rank Math WordPress SEO plugin"></head></html>') == "rank_math"
    assert seo_plugin("<html><head><title>plain</title></head><body>no plugin</body></html>") is None


def test_plugin_install_unblocks_a_plugin_skip():
    skipped = {"skipped": {"reason": "Core WordPress has no meta description field; needs an SEO plugin such as Yoast."}}
    assert site_audit.plugin_unblocks_skip(skipped, {"facts": {"seo_plugin": "yoast"}})
    assert not site_audit.plugin_unblocks_skip(skipped, {"facts": {"seo_plugin": None}})
    assert not site_audit.plugin_unblocks_skip({"skipped": {"reason": "Controlled by the theme."}}, {"facts": {"seo_plugin": "yoast"}})
    assert not site_audit.plugin_unblocks_skip({}, {"facts": {"seo_plugin": "yoast"}})

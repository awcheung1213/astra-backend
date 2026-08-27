"""Audit rule engine — pure functions over fetched text."""

from utils.tasks.site_audit import KIND_MISSING_H1, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_SCHEMA, KIND_MISSING_TITLE, KIND_SLOW_PAGE, extract_internal_links, finding_key, page_findings, robots_blocked_agents

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

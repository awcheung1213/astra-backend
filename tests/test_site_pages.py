"""The "Your site" screen at scale — pure tests (monkeypatch only, no DB, no
network): sitemap parsing + caps, section naming, the 25-page rotation, page
status, the resolve-only-checked-pages invariant, the worklog rollups, and
the batch approve's per-item shaping."""

import contextlib
import gzip
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.site_pages as site_pages_api
import worker.tasks.site_audit as site_audit
from objects.finding import STATUS_FIXED
from utils import site_overview
from utils.site_fetch import decode_body
from worker.tasks.site_audit import CrawlResult, PageState, keys_to_resolve, page_status, parse_sitemap, path_section, read_sitemaps, rotation_order, same_site_url, sitemap_section

BASE = "https://reyes.example"
HOME = "https://reyes.example/"


async def _async(value):
    return value


async def _none(*args, **kwargs):
    return None


def _urlset(*urls: str) -> str:
    return '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>"


def _index(*urls: str) -> str:
    return '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls) + "</sitemapindex>"


##############################
# SITEMAP PARSING
##############################


def test_parse_urlset_takes_each_entrys_own_loc_only():
    body = "<urlset><url><loc> https://reyes.example/a?x=1&amp;y=2 </loc><image:image><image:loc>https://reyes.example/pic.jpg</image:loc></image:image></url><url><loc><![CDATA[https://reyes.example/b/]]></loc></url><url><loc>https://reyes.example/b/</loc></url></urlset>"
    assert parse_sitemap(body) == ("urlset", ["https://reyes.example/a?x=1&y=2", "https://reyes.example/b/"])


def test_parse_index_and_non_sitemaps():
    assert parse_sitemap(_index("https://reyes.example/page-sitemap.xml", "https://reyes.example/post-sitemap.xml")) == ("index", ["https://reyes.example/page-sitemap.xml", "https://reyes.example/post-sitemap.xml"])
    assert parse_sitemap("<html><body>Not found</body></html>") == (None, [])
    assert parse_sitemap(None) == (None, [])


def test_gzip_sitemap_bodies_are_decoded():
    assert decode_body(gzip.compress(_urlset(HOME).encode())) == _urlset(HOME)
    assert decode_body(b"plain") == "plain"
    assert decode_body(gzip.compress(b"x" * 100), max_bytes=10) == "x" * 10  # capped: no zip bombs


async def test_read_sitemaps_walks_indexes_same_site_only_and_names_sections():
    bodies = {
        "https://reyes.example/wp-sitemap-posts-page-1.xml": _urlset(HOME, "https://www.reyes.example/about/", "https://other.example/x/"),
        "https://reyes.example/wp-sitemap-posts-post-1.xml": _urlset("https://reyes.example/2026/10/leak-tips/"),
        "https://reyes.example/wp-sitemap-posts-service_area-1.xml": _urlset("https://reyes.example/service-areas/tempe/"),
        "https://reyes.example/nested-index.xml": _index("https://reyes.example/wp-sitemap.xml", "https://reyes.example/wp-sitemap-taxonomies-category-1.xml"),  # a cycle back to the root is skipped
        "https://reyes.example/wp-sitemap-taxonomies-category-1.xml": _urlset("https://reyes.example/category/news/"),
    }
    fetched: list[str] = []

    async def fetch_body(url):
        fetched.append(url)
        return bodies.get(url)

    root = _index(*[url for url in bodies if "taxonomies" not in url], "https://cdn.other.example/sitemap.xml")
    listed = await read_sitemaps([("https://reyes.example/wp-sitemap.xml", root)], BASE, fetch_body)
    assert listed == {
        HOME: "Main pages",  # the CMS "Pages" type groups by address
        "https://reyes.example/about/": "Main pages",  # www spelling folded onto the brand's own host
        "https://reyes.example/2026/10/leak-tips/": "Blog posts",
        "https://reyes.example/service-areas/tempe/": "Service areas",
        "https://reyes.example/category/news/": "Categories",
    }
    assert "https://cdn.other.example/sitemap.xml" not in fetched and fetched.count("https://reyes.example/wp-sitemap.xml") == 0


async def test_read_sitemaps_caps_files_and_urls(monkeypatch):
    monkeypatch.setattr(site_audit, "MAX_SITEMAP_FILES", 3)
    monkeypatch.setattr(site_audit, "MAX_SITEMAP_URLS", 5)
    children = [f"https://reyes.example/sitemap-{n}.xml" for n in range(10)]
    fetched: list[str] = []

    async def fetch_body(url):
        fetched.append(url)
        number = url.rsplit("-", 1)[-1].split(".")[0]
        return _urlset(*[f"https://reyes.example/p{number}-{i}/" for i in range(2)])

    listed = await read_sitemaps([("https://reyes.example/sitemap.xml", _index(*children))], BASE, fetch_body)
    assert len(fetched) == 2  # root + 2 children = the 3-file cap
    assert len(listed) == 4

    monkeypatch.setattr(site_audit, "MAX_SITEMAP_FILES", 50)
    listed = await read_sitemaps([("https://reyes.example/sitemap.xml", _index(*children))], BASE, fetch_body)
    assert len(listed) == 5  # url cap


##############################
# SECTIONS
##############################


@pytest.mark.parametrize(
    "sitemap_url, section",
    [
        ("https://s.example/wp-sitemap-posts-page-1.xml", None),
        ("https://s.example/wp-sitemap-posts-post-2.xml", "Blog posts"),
        ("https://s.example/wp-sitemap-posts-service_area-1.xml", "Service areas"),
        ("https://s.example/wp-sitemap-posts-service-1.xml", "Services"),
        ("https://s.example/wp-sitemap-posts-city-1.xml", "Cities"),
        ("https://s.example/wp-sitemap-taxonomies-category-1.xml", "Categories"),
        ("https://s.example/wp-sitemap-taxonomies-post_tag-1.xml", "Categories"),
        ("https://s.example/page-sitemap.xml", None),
        ("https://s.example/post-sitemap2.xml", "Blog posts"),
        ("https://s.example/product-sitemap.xml", "Products"),
        ("https://s.example/product_cat-sitemap.xml", "Categories"),
        ("https://s.example/category-sitemap.xml", "Categories"),
        ("https://s.example/service-sitemap.xml", "Services"),
        ("https://shop.example/sitemap_products_1.xml?from=1&to=999", "Products"),
        ("https://shop.example/sitemap_pages_1.xml", None),
        ("https://shop.example/sitemap_collections_1.xml", "Collections"),
        ("https://shop.example/sitemap_blogs_1.xml", "Blog posts"),
        ("https://s.example/page-sitemap.xml.gz", None),
        ("https://s.example/sitemap.xml", None),
        ("https://s.example/sitemap_index.xml", None),
        ("https://s.example/local-sitemap.xml", None),
    ],
)
def test_sitemap_file_names_that_say_what_they_list(sitemap_url, section):
    assert sitemap_section(sitemap_url) == section


def test_path_fallback_sections():
    assert path_section(HOME) == "Main pages"
    assert path_section("https://reyes.example/about/") == "Main pages"
    assert path_section("https://reyes.example/contact-us") == "Main pages"
    assert path_section("https://reyes.example/service-areas/tempe/") == "Service areas"
    assert path_section("https://reyes.example/blog/how_to_fix-a-leak/") == "Blog"


def test_same_site_url():
    assert same_site_url("http://www.reyes.example/About/#x", BASE) == "https://reyes.example/About/"
    assert same_site_url("https://reyes.example", BASE) == HOME
    assert same_site_url("https://other.example/", BASE) is None
    assert same_site_url("mailto:hi@reyes.example", BASE) is None


def test_link_crawl_counts_pages_not_files_admin_or_variants():
    html = '<a href="/about/">A</a><a href="services/drains/">rel</a><a href="/wp-admin/">x</a><a href="/menu.pdf">pdf</a><a href="/shop?page=2">p</a><a href="https://other.example/">o</a><a href="#top">t</a>'
    assert site_audit.page_links("https://reyes.example/services/", BASE, html) == ["https://reyes.example/about/", "https://reyes.example/services/services/drains/", "https://reyes.example/services/"]


##############################
# ROTATION + STATUS
##############################


def test_rotation_homepage_then_new_main_pages_then_new_others_then_oldest_checked():
    long_ago, last_week = datetime(2026, 8, 1), datetime(2026, 10, 7)
    pages = {
        "https://reyes.example/blog/a/": PageState("https://reyes.example/blog/a/", "Blog", first_seen_at=long_ago, order=3),
        "https://reyes.example/about/": PageState("https://reyes.example/about/", "Main pages", first_seen_at=last_week, order=9),
        "https://reyes.example/old/": PageState("https://reyes.example/old/", "Main pages", first_seen_at=long_ago, last_checked_at=datetime(2026, 9, 1)),
        "https://reyes.example/older/": PageState("https://reyes.example/older/", "Blog", first_seen_at=long_ago, last_checked_at=datetime(2026, 8, 20)),
        HOME: PageState(HOME, "Main pages", first_seen_at=long_ago, last_checked_at=last_week),
        "https://reyes.example/contact/": PageState("https://reyes.example/contact/", "Pages", first_seen_at=last_week, order=1),
    }
    assert rotation_order(pages, HOME, limit=10) == [HOME, "https://reyes.example/contact/", "https://reyes.example/about/", "https://reyes.example/blog/a/", "https://reyes.example/older/", "https://reyes.example/old/"]
    assert rotation_order(pages, HOME, limit=2, exclude={HOME}) == ["https://reyes.example/contact/", "https://reyes.example/about/"]


def test_page_status_from_what_the_run_detected():
    assert page_status([]) == "healthy"
    assert page_status(["missing_meta_description", "slow_page"]) == "needs_fix"
    assert page_status(["slow_page"]) == "slow"
    assert page_status(["broken_link"]) == "broken"


##############################
# THE INVARIANT: only checked pages auto-resolve
##############################


def test_only_checked_pages_and_site_wide_checks_resolve():
    open_by_key = {
        ("missing_schema", "https://reyes.example/checked/"): "f1",  # checked, gone -> resolve
        ("missing_schema", "https://reyes.example/not-checked/"): "f2",  # outside the rotation -> stays open
        ("missing_title", "https://reyes.example/checked/"): "f3",  # checked, still there -> stays open
        ("blocked_ai_crawler", "https://reyes.example/robots.txt"): "f4",  # site-wide, gone -> resolve
        ("broken_link", "https://reyes.example/dead/"): "f5",  # probed, loads now -> resolve
        ("broken_link", "https://reyes.example/other-dead/"): "f6",  # not looked at -> stays open
    }
    detected = {("missing_title", "https://reyes.example/checked/")}
    resolved = keys_to_resolve(open_by_key, detected, {"https://reyes.example/checked/"}, {"https://reyes.example/dead/"})
    assert sorted(open_by_key[key] for key in resolved) == ["f1", "f4", "f5"]


def _patch_audit(monkeypatch, crawl_result: CrawlResult, open_findings: list, recorded: dict):
    async def crawl(base_url, known_pages=(), problem_urls=()):
        recorded["problem_urls"] = list(problem_urls)
        return crawl_result

    async def resolve(id, status=STATUS_FIXED):
        recorded.setdefault("resolved", []).append(id)

    async def upsert_seen(brand_id, sections_by_url, seen_at):
        recorded["seen"] = dict(sections_by_url)

    async def record_checks(brand_id, checks, checked_at):
        recorded["checks"] = list(checks)

    async def create(**fields):
        recorded.setdefault("created", []).append(fields)
        return SimpleNamespace(id=f"new-{len(recorded['created'])}")

    async def create_task(**fields):
        recorded.setdefault("tasks", []).append(fields)

    monkeypatch.setattr(site_audit.Brand, "get", lambda brand_id: _async(SimpleNamespace(id="b1", website_url=BASE)))
    monkeypatch.setattr(site_audit.Site, "by_brand", _none)
    monkeypatch.setattr(site_audit, "_crawl", crawl)
    monkeypatch.setattr(site_audit.Finding, "open_for_brand", lambda brand_id: _async(open_findings))
    monkeypatch.setattr(site_audit.Finding, "fixed_by_change_for_brand", lambda brand_id: _async([]))
    monkeypatch.setattr(site_audit.Finding, "resolve", resolve)
    monkeypatch.setattr(site_audit.Finding, "create", create)
    monkeypatch.setattr(site_audit, "create_task", create_task)
    monkeypatch.setattr(site_audit.SitePage, "for_brand", lambda brand_id: _async([]))
    monkeypatch.setattr(site_audit.SitePage, "upsert_seen", upsert_seen)
    monkeypatch.setattr(site_audit.SitePage, "record_checks", record_checks)
    monkeypatch.setattr(site_audit, "queue_first_plan_when_ready", _none)


async def test_audit_never_resolves_findings_on_pages_it_did_not_check(monkeypatch):
    def finding(id, kind, url):
        return SimpleNamespace(id=id, kind=kind, detail={"url": url}, created_at=datetime(2026, 9, 1))

    open_findings = [finding("f-checked", "missing_h1", "https://reyes.example/a/"), finding("f-unchecked", "missing_h1", "https://reyes.example/b/"), finding("f-sitemap", "missing_sitemap", "https://reyes.example/sitemap.xml")]
    crawl_result = CrawlResult(findings=[{"kind": "missing_schema", "severity": "warn", "summary": "s", "detail": {"url": "https://reyes.example/a/"}}], seen={HOME: "Main pages"}, checks=[(HOME, "Main pages", "healthy"), ("https://reyes.example/a/", "Main pages", "needs_fix")], from_sitemap=True)
    recorded: dict = {}
    _patch_audit(monkeypatch, crawl_result, open_findings, recorded)
    result = await site_audit.run_site_audit("b1")
    assert sorted(recorded["resolved"]) == ["f-checked", "f-sitemap"]  # f-unchecked keeps its open problem
    assert recorded["problem_urls"] == ["https://reyes.example/a/", "https://reyes.example/b/"]  # page problems keep their pages current
    assert recorded["checks"] == crawl_result.checks and recorded["seen"] == {HOME: "Main pages"}
    assert result == {"detected": 1, "created": 1, "resolved": 2, "checked": 2}
    assert recorded["tasks"][0]["payload"] == {"finding_ids": ["new-1"]}


##############################
# THE CRAWL (fake fetch)
##############################


def _response(body: str, content_type: str = "text/html", status_code: int = 200):
    return SimpleNamespace(status_code=status_code, headers={"content-type": content_type}, text=body)


@contextlib.asynccontextmanager
async def _fake_session(**kwargs):
    yield None


PAGE = '<html><head><title>T</title><meta name="description" content="d"><script type="application/ld+json">{"@type":"Plumber"}</script></head><body><h1>H</h1>{links}</body></html>'


async def test_crawl_checks_at_most_max_pages_from_the_sitemap_in_rotation_order(monkeypatch):
    monkeypatch.setattr(site_audit, "MAX_PAGES", 3)
    listed = [HOME, "https://reyes.example/about/", "https://reyes.example/blog/a/", "https://reyes.example/blog/b/"]
    fetched: list[str] = []

    async def fake_fetch(client, url, fresh=False):
        fetched.append(url)
        if url.endswith("/robots.txt"):
            return _response("User-agent: *\nDisallow:\nSitemap: https://reyes.example/sitemap.xml", "text/plain")
        if url == "https://reyes.example/sitemap.xml":
            return _response(_urlset(*listed), "application/xml")
        return _response(PAGE.replace("{links}", ""))

    monkeypatch.setattr(site_audit, "_fetch", fake_fetch)
    monkeypatch.setattr(site_audit, "site_session", _fake_session)
    known = [SimpleNamespace(url="https://reyes.example/blog/a/", section="Blog", first_seen_at=datetime(2026, 9, 1), last_seen_at=datetime.now(), last_checked_at=datetime.now() - timedelta(days=7))]
    result = await site_audit._crawl(BASE, known, problem_urls=[])
    assert result.from_sitemap and set(result.seen) == set(listed)
    assert [url for url, _, _ in result.checks] == [HOME, "https://reyes.example/about/", "https://reyes.example/blog/b/"]  # blog/a was checked last week: it waits
    assert {status for _, _, status in result.checks} == {"healthy"} and result.findings == []


async def test_crawl_probes_unlisted_links_with_spare_budget_and_keeps_problem_pages(monkeypatch):
    async def fake_fetch(client, url, fresh=False):
        if url.endswith("/robots.txt"):
            return None
        if url == "https://reyes.example/sitemap.xml":
            return _response(_urlset(HOME), "text/xml")
        if url == "https://reyes.example/gone/":
            return _response("nope", status_code=404)
        return _response(PAGE.replace("{links}", '<a href="/gone/">old</a>'))

    monkeypatch.setattr(site_audit, "_fetch", fake_fetch)
    monkeypatch.setattr(site_audit, "site_session", _fake_session)
    result = await site_audit._crawl(BASE, [], problem_urls=["https://reyes.example/cart/"])
    assert result.seen == {HOME: "Main pages", "https://reyes.example/cart/": "Main pages"}
    assert [url for url, _, _ in result.checks] == [HOME, "https://reyes.example/cart/"]
    assert result.probed == {"https://reyes.example/gone/"}
    assert [(finding["kind"], finding["detail"]["url"]) for finding in result.findings] == [("broken_link", "https://reyes.example/gone/")]


async def test_no_sitemap_falls_back_to_the_link_crawl(monkeypatch):
    async def fake_fetch(client, url, fresh=False):
        if url.endswith("robots.txt") or "sitemap" in url:
            return None
        if url == "https://reyes.example/service-areas/tempe/":
            return None  # unreachable
        return _response(PAGE.replace("{links}", '<a href="/about/">a</a><a href="/service-areas/tempe/">t</a>'))

    monkeypatch.setattr(site_audit, "_fetch", fake_fetch)
    monkeypatch.setattr(site_audit, "site_session", _fake_session)
    result = await site_audit._crawl(BASE)
    assert not result.from_sitemap
    assert result.checks == [(HOME, "Main pages", "healthy"), ("https://reyes.example/about/", "Main pages", "healthy"), ("https://reyes.example/service-areas/tempe/", "Service areas", "broken")]
    assert result.seen == {HOME: "Main pages", "https://reyes.example/about/": "Main pages", "https://reyes.example/service-areas/tempe/": "Service areas"}
    assert {finding["kind"] for finding in result.findings} == {"missing_sitemap", "broken_link"}


##############################
# WORKLOG ROLLUPS
##############################


def test_site_pages_summary_counts_sections_and_weeks_to_full():
    checked = datetime(2026, 10, 12, 9, 0)
    rows = [
        ("Service areas", "unchecked", 100, None),
        ("Service areas", "healthy", 40, checked),
        ("Service areas", "needs_fix", 4, checked - timedelta(days=7)),
        ("Blog posts", "unchecked", 39, None),
        ("Blog posts", "slow", 2, checked),
        ("Main pages", "healthy", 27, checked),
        ("Main pages", "needs_fix", 2, checked),
    ]
    summary = site_overview.summarize_site_pages(rows)
    assert {key: summary[key] for key in ("total", "checked", "healthy", "needs_fix", "slow", "broken", "per_week", "weeks_to_full")} == {"total": 214, "checked": 75, "healthy": 67, "needs_fix": 6, "slow": 2, "broken": 0, "per_week": 25, "weeks_to_full": 6}
    assert [(entry["section"], entry["total"], entry["checked"], entry["healthy"], entry["needs_fix"], entry["slow"]) for entry in summary["by_section"]] == [("Service areas", 144, 44, 40, 4, 0), ("Blog posts", 41, 2, 0, 0, 2), ("Main pages", 29, 29, 27, 2, 0)]
    assert summary["last_checked_at"] == "2026-10-12T09:00:00+00:00"
    assert site_overview.summarize_site_pages([]) == {"total": 0, "checked": 0, "healthy": 0, "needs_fix": 0, "slow": 0, "broken": 0, "per_week": 25, "weeks_to_full": 0, "by_section": [], "last_checked_at": None}


def _finding(id, kind, url, status="open", detail=None):
    return SimpleNamespace(id=id, kind=kind, severity="warn", summary=f"summary {id}", status=status, detail={"url": url, **(detail or {})}, created_at=datetime(2026, 10, 1), resolved_at=datetime(2026, 10, 10) if status == "fixed" else None)


def _proposal(id, finding_id, status="pending_review"):
    return SimpleNamespace(id=id, status=status, content={"finding_id": finding_id, "operations": [{"op": "set_field"}]}, created_at=datetime(2026, 10, 2))


def test_issue_groups_by_problem_with_ready_fix_ids():
    findings = [
        _finding("d1", "missing_meta_description", "https://reyes.example/"),
        _finding("d2", "missing_meta_description", "https://reyes.example/about/"),
        _finding("d3", "missing_meta_description", "https://reyes.example/contact/"),
        _finding("h1", "missing_h1", "https://reyes.example/about/"),
        _finding("s1", "slow_page", "https://reyes.example/blog/a/", detail={"elapsed_seconds": 4.2}),
        _finding("t1", "missing_title", "https://reyes.example/old/", status="fixed"),
    ]
    proposals = [_proposal("a1", "d1"), _proposal("a2", "d2"), _proposal("a3", "d3", status="rejected"), _proposal("a1", "d1")]
    groups = site_overview.issue_groups(findings, proposals, [])
    assert groups == [
        {"kind": "missing_meta_description", "title": "No Google description", "pages": 3, "fixes_ready": 2, "artifact_ids": ["a1", "a2"], "status": "fix_ready"},
        {"kind": "missing_h1", "title": "No main heading", "pages": 1, "fixes_ready": 0, "artifact_ids": [], "status": "needs_fix"},
        {"kind": "slow_page", "title": "Slow to load", "pages": 1, "fixes_ready": 0, "artifact_ids": [], "status": "cant_fix"},
        {"kind": "missing_title", "title": "No Google title", "pages": 1, "fixes_ready": 0, "artifact_ids": [], "status": "fixed"},
    ]


def test_issue_group_titles_pass_the_jargon_check():
    from utils.receipts import has_jargon

    assert not any(has_jargon(title) for title in site_overview.ISSUE_GROUP_TITLES.values())


def test_page_search_rows_use_owner_words():
    page = SimpleNamespace(url="https://reyes.example/about-us/", section="Main pages", status="needs_fix", last_checked_at=datetime(2026, 10, 12))
    assert site_overview.page_entry(page) == {"url": "https://reyes.example/about-us/", "label": "About us page", "section": "Main pages", "status": "needs_fix", "last_checked_at": "2026-10-12T00:00:00+00:00"}


##############################
# BATCH APPROVE
##############################


def test_batch_refusals_and_result_shape():
    mine = SimpleNamespace(id="a1", brand_id="b1", type="change_proposal")
    assert site_pages_api.batch_refusal(mine, "b1") is None
    assert site_pages_api.batch_refusal(None, "b1") == site_pages_api.DETAIL_NOT_FOUND
    assert site_pages_api.batch_refusal(SimpleNamespace(id="a2", brand_id="b2", type="change_proposal"), "b1") == site_pages_api.DETAIL_NOT_FOUND  # another brand's id looks missing
    assert site_pages_api.batch_refusal(SimpleNamespace(id="a3", brand_id="b1", type="pitch"), "b1") == site_pages_api.DETAIL_REVIEW_ON_ITS_OWN  # floor types never go in bulk
    assert site_pages_api.batch_result("a1", True, "ignored") == {"artifact_id": "a1", "ok": True, "detail": None}
    assert site_pages_api.batch_result("a2", False, "nope") == {"artifact_id": "a2", "ok": False, "detail": "nope"}
    assert site_pages_api.unique_ids(["a1", "a2", "a1", ""]) == ["a1", "a2"]


def test_batch_request_caps_at_fifty_ids():
    from pydantic import ValidationError

    site_pages_api.BatchApprovalRequest(artifact_ids=["a"] * 50)
    with pytest.raises(ValidationError):
        site_pages_api.BatchApprovalRequest(artifact_ids=["a"] * 51)
    with pytest.raises(ValidationError):
        site_pages_api.BatchApprovalRequest(artifact_ids=[])


async def test_batch_approve_runs_each_id_through_the_single_review_path(monkeypatch):
    artifacts = [SimpleNamespace(id=f"a{n}", brand_id="b1", type="change_proposal") for n in range(1, 4)] + [SimpleNamespace(id="p1", brand_id="b1", type="reply")]
    reviewed: list[str] = []
    savepoints: list[str] = []

    async def review_artifact(artifact_id, request, http_request):
        assert request.decision == "approve"
        if artifact_id == "a2":
            raise HTTPException(status_code=409, detail="artifact is approved, not pending review")
        if artifact_id == "a3":
            raise RuntimeError("boom")
        reviewed.append(artifact_id)

    class FakeSession:
        @contextlib.asynccontextmanager
        async def begin_nested(self):
            savepoints.append("open")
            yield

    monkeypatch.setattr(site_pages_api.Brand, "get", lambda brand_id: _async(SimpleNamespace(id=brand_id)))
    monkeypatch.setattr(site_pages_api.Artifact, "by_ids", lambda ids: _async(artifacts))
    monkeypatch.setattr(site_pages_api, "review_artifact", review_artifact)
    monkeypatch.setattr(site_pages_api, "Database", SimpleNamespace(session=FakeSession()))
    body = site_pages_api.BatchApprovalRequest(artifact_ids=["a1", "a2", "a3", "p1", "x9", "a1"])
    response = await site_pages_api.approve_batch("b1", body, SimpleNamespace())
    assert response == {
        "results": [
            {"artifact_id": "a1", "ok": True, "detail": None},
            {"artifact_id": "a2", "ok": False, "detail": "artifact is approved, not pending review"},
            {"artifact_id": "a3", "ok": False, "detail": site_pages_api.DETAIL_FAILED},
            {"artifact_id": "p1", "ok": False, "detail": site_pages_api.DETAIL_REVIEW_ON_ITS_OWN},
            {"artifact_id": "x9", "ok": False, "detail": site_pages_api.DETAIL_NOT_FOUND},
        ]
    }
    assert reviewed == ["a1"] and len(savepoints) == 3  # refused-up-front ids never open a savepoint

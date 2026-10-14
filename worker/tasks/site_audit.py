"""The site audit (#3 ongoing): sitemap -> page rotation -> audit rule
engine -> typed findings.

Deterministic tier-1 code only — no LLM, $0. Two outputs:
- site_pages: the site's TRUE page list (every sitemap-listed page; no
  sitemap -> the link crawl's pages) with a per-page status. Each run CHECKS
  at most MAX_PAGES of them (weekly cron + the owner's "Check again"),
  rotating — homepage first, then never-checked main pages, then other
  never-checked pages, then the oldest-checked — so every page gets its turn.
- the findings table (open -> fixed | dismissed):
  - NEW fixable findings chain ONE technical_fix task per audit carrying their
    ids (#9 — this is the producer the schedules file points at); that task
    proposes fixes (generators or the Technical agent, per
    settings.TECHNICAL_FIX_MODE). Every finding also carries the page FACTS the
    crawler saw (title, metas, H1s, JSON-LD types, timing) so the fixer reads
    what the audit read instead of re-crawling.
  - Open audit-managed findings that are NO LONGER detected auto-resolve
    (status fixed) — but ONLY for pages this run actually checked (plus the
    site-wide robots/sitemap checks, which run every time). A page outside
    this run's rotation keeps its open findings: not looking is not fixed.
  - A finding a live fix closed (worker/tasks/changes.py) that the crawl still
    detects is REOPENED with a fix_did_not_take marker, never duplicated.

Crawling the client's PUBLIC site is a free, credential-less read, so it goes
through utils/site_fetch (browser-fingerprinted curl_cffi — see that module for
why not httpx) rather than an adapter — move it behind a crawler adapter when
rate limits or metering ever matter. The checks, sitemap parsing, section
naming and rotation order are pure functions (see tests/test_site_audit.py,
tests/test_site_pages.py)."""

import heapq
import html as html_entities
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

from curl_cffi.requests import AsyncSession

from objects.brand import Brand
from objects.finding import SEVERITY_SERIOUS, SEVERITY_WARN, STATUS_DUPLICATE, Finding
from objects.site import Site
from objects.site_page import STATUS_BROKEN, STATUS_HEALTHY, STATUS_NEEDS_FIX, STATUS_SLOW, SitePage, current_since
from utils import clock
from utils.first_plan import queue_first_plan_when_ready
from utils.logger import logger
from utils.site_fetch import body_text
from utils.site_fetch import fetch as _fetch
from utils.site_fetch import site_session
from utils.task_queue import RETRY_WITH_BACKOFF, create_task, task_lifecycle
from worker.app import app


@app.task(name="site_audit", queue="audit", retry=RETRY_WITH_BACKOFF)
async def site_audit(task_id: str, brand_id: str) -> None:
    """#3 (ongoing) — crawl + audit rule engine over the brand's connected
    site -> typed findings. Fires at onboarding (site connect) and weekly via
    schedule_site_audits. Chains #9: the run's NEW fixable findings ride ONE
    technical_fix task (inside run_site_audit). Idempotent: re-detected
    findings dedupe, vanished ones on CHECKED pages auto-resolve — retries
    are safe (a retry just advances the rotation)."""
    async with task_lifecycle(task_id):
        await run_site_audit(brand_id, completing_task_id=task_id)


MAX_PAGES = 25  # pages CHECKED per run; the page list itself is uncapped up to MAX_SITEMAP_URLS
MAX_SITEMAP_FILES = 50  # sitemap documents read per run (index + children)
MAX_SITEMAP_URLS = 5000  # pages a site's list holds (sitemap or link crawl)
FETCH_TIMEOUT_SECONDS = 10.0
SLOW_PAGE_SECONDS = 3.0

# the crawlers whose access decides AI visibility — blocking them is the
# single most self-defeating thing a client site can do
AI_CRAWLERS = ("GPTBot", "ClaudeBot", "Google-Extended", "PerplexityBot")

# where a sitemap lives when robots.txt doesn't say: the classic path, then the
# Yoast/Rank Math index, then WordPress core's built-in (5.5+) sitemap
DEFAULT_SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")

KIND_BLOCKED_AI_CRAWLER = "blocked_ai_crawler"
KIND_MISSING_SITEMAP = "missing_sitemap"
KIND_MISSING_TITLE = "missing_title"
KIND_MISSING_META_DESCRIPTION = "missing_meta_description"
KIND_MISSING_H1 = "missing_h1"
KIND_MISSING_SCHEMA = "missing_schema"
KIND_SLOW_PAGE = "slow_page"
KIND_BROKEN_LINK = "broken_link"

# every kind this engine manages — the auto-resolve diff only touches these,
# so human/detector findings are never resolved by a crawl
AUDIT_KINDS = {KIND_BLOCKED_AI_CRAWLER, KIND_MISSING_SITEMAP, KIND_MISSING_TITLE, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_H1, KIND_MISSING_SCHEMA, KIND_SLOW_PAGE, KIND_BROKEN_LINK}
# kind -> mechanically fixable via the CMS write path; the rest stay findings
# until a fix generator or the Technical agent knows what to do with them
FIXABLE_KINDS = {KIND_MISSING_TITLE, KIND_MISSING_META_DESCRIPTION, KIND_MISSING_H1, KIND_MISSING_SCHEMA}
# checked EVERY run (robots.txt + sitemap discovery), so absence = resolved;
# every other audit kind is about one page and resolves only when that page
# was checked this run (keys_to_resolve)
SITE_WIDE_AUDIT_KINDS = {KIND_BLOCKED_AI_CRAWLER, KIND_MISSING_SITEMAP}
PAGE_KINDS = AUDIT_KINDS - SITE_WIDE_AUDIT_KINDS


##############################
# RULE ENGINE (pure)
##############################


def robots_blocked_agents(robots_txt: str) -> list[str]:
    """AI crawlers fully disallowed by robots.txt. Simple section parser:
    a `Disallow: /` under an agent's own section (or under `*` with no agent
    section of its own) blocks that agent."""
    disallow_rules_by_agent: dict[str, list[str]] = {}
    section_agents: list[str] = []
    previous_line_was_user_agent = False
    for raw_line in robots_txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, directive_value = (part.strip() for part in line.split(":", 1))
        if directive.lower() == "user-agent":
            if not previous_line_was_user_agent:
                section_agents = []  # a fresh section starts
            section_agents.append(directive_value.lower())
            disallow_rules_by_agent.setdefault(directive_value.lower(), [])
            previous_line_was_user_agent = True
        else:
            previous_line_was_user_agent = False
            if directive.lower() == "disallow":
                for agent in section_agents:
                    disallow_rules_by_agent[agent].append(directive_value)

    blocked_crawlers = []
    for crawler in AI_CRAWLERS:
        crawler_rules = disallow_rules_by_agent.get(crawler.lower(), disallow_rules_by_agent.get("*", []))
        if any(rule == "/" for rule in crawler_rules):
            blocked_crawlers.append(crawler)
    return blocked_crawlers


def robots_sitemap_urls(robots_txt: str) -> list[str]:
    """Absolute URLs named by `Sitemap:` directives, in order, deduped. The
    directive is section-independent, so every occurrence counts."""
    sitemap_urls: list[str] = []
    for raw_line in robots_txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        directive, directive_value = (part.strip() for part in line.split(":", 1))
        is_absolute_url = urlparse(directive_value).scheme in ("http", "https")
        if directive.lower() == "sitemap" and is_absolute_url and directive_value not in sitemap_urls:
            sitemap_urls.append(directive_value)
    return sitemap_urls


def sitemap_candidate_urls(base_url: str, robots_txt: str | None) -> list[str]:
    """Where to look for a sitemap, most authoritative first: robots.txt's own
    declaration(s), then the conventional paths. Deduped in order."""
    candidates: list[str] = []
    for url in robots_sitemap_urls(robots_txt or "") + [urljoin(base_url, path) for path in DEFAULT_SITEMAP_PATHS]:
        if url not in candidates:
            candidates.append(url)
    return candidates


def is_sitemap_document(content_type: str, body: str) -> bool:
    """A 200 alone doesn't prove a sitemap — soft-404s and HTML fallbacks are
    common — so require sitemap XML (a urlset or a sitemap index)."""
    if "html" in content_type.lower():
        return False
    return bool(re.search(r"<(urlset|sitemapindex)[\s>]", body[:4096], re.I))


def _finding(kind: str, severity: str, summary: str, url: str, **extra_detail) -> dict:
    return {"kind": kind, "severity": severity, "summary": summary, "detail": {"url": url, **extra_detail}}


def page_facts(html: str, elapsed_seconds: float) -> dict:
    """What the crawler SAW on a page — the shared vocabulary between the rule
    engine (which turns absences into findings) and the fixer (which reads
    these off the finding instead of re-crawling). Pure."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    # meta attributes appear in either order: name-then-content or content-then-name
    meta_match = re.search(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"']([^\"']*)[\"']", html, re.I) or re.search(r"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]+name=[\"']description[\"']", html, re.I)
    h1s = [_strip_tags(match) for match in re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)]
    ld_json_blocks = re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.I | re.S)
    return {
        "title": _strip_tags(title_match.group(1)) if title_match else "",
        "meta_description": (meta_match.group(1) or "").strip() if meta_match else "",
        "h1s": [heading for heading in h1s if heading],
        "ld_json_types": _ld_json_types(ld_json_blocks),
        "word_count": len(visible_text(html).split()),
        "seo_plugin": seo_plugin(html),
        "elapsed_seconds": round(elapsed_seconds, 2),
    }


# markers the common WordPress SEO plugins leave in the rendered head — the
# fixer needs this because core WordPress has no meta-description field: an
# excerpt only becomes <meta name="description"> when one of these renders it
SEO_PLUGIN_MARKERS = (("yoast", "yoast"), ("rank math", "rank_math"), ("rank-math", "rank_math"), ("aioseo", "aioseo"), ("all in one seo", "aioseo"), ("seopress", "seopress"), ("the seo framework", "seo_framework"))


def seo_plugin(html: str) -> str | None:
    """Which SEO plugin rendered this page, by its head markers; None when none."""
    head = html[:20000].lower()
    for marker, name in SEO_PLUGIN_MARKERS:
        if marker in head:
            return name
    return None


def plugin_unblocks_skip(existing_detail: dict | None, detected_detail: dict | None) -> bool:
    """A fix skipped for want of an SEO plugin, on a page that now renders one:
    the owner did their part (the issue sheet links the install), so retry. Pure."""
    skipped = (existing_detail or {}).get("skipped") or {}
    reason = str(skipped.get("reason") or "").lower()
    if not skipped or not any(word in reason for word in ("plugin", "yoast", "rank math", "rank_math", "aioseo", "seopress")):
        return False
    return bool(((detected_detail or {}).get("facts") or {}).get("seo_plugin"))


def page_findings(url: str, html: str, elapsed_seconds: float) -> list[dict]:
    """Deterministic per-page checks -> finding dicts (kind, severity, summary,
    detail); every detail carries the page's facts."""
    facts = page_facts(html, elapsed_seconds)
    is_slow = elapsed_seconds > SLOW_PAGE_SECONDS

    findings: list[dict] = []
    if not facts["title"]:
        findings.append(_finding(KIND_MISSING_TITLE, SEVERITY_WARN, f"Page has no <title>: {url}", url, facts=facts))
    if not facts["meta_description"]:
        findings.append(_finding(KIND_MISSING_META_DESCRIPTION, SEVERITY_WARN, f"Page has no meta description: {url}", url, facts=facts))
    if not facts["h1s"]:
        findings.append(_finding(KIND_MISSING_H1, SEVERITY_WARN, f"Page has no <h1>: {url}", url, facts=facts))
    if not ld_json_blocks_present(html):
        findings.append(_finding(KIND_MISSING_SCHEMA, SEVERITY_WARN, f"Page has no structured data (ld+json): {url}", url, facts=facts))
    if is_slow:
        findings.append(_finding(KIND_SLOW_PAGE, SEVERITY_SERIOUS, f"Page took {elapsed_seconds:.1f}s to respond: {url}", url, elapsed_seconds=round(elapsed_seconds, 2), facts=facts))
    return findings


def ld_json_blocks_present(html: str) -> bool:
    return bool(re.search(r"<script[^>]+application/ld\+json", html, re.I))


def visible_text(html: str) -> str:
    """The page's readable text: scripts/styles and tags stripped, whitespace collapsed."""
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    return _strip_tags(body)


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _ld_json_types(blocks: list[str]) -> list[str]:
    """The @type values declared across a page's JSON-LD blocks (graphs included)."""
    types: list[str] = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for item in [node] + [graph_node for graph_node in node.get("@graph", []) if isinstance(graph_node, dict)]:
                declared = item.get("@type")
                for type_name in declared if isinstance(declared, list) else [declared]:
                    if isinstance(type_name, str) and type_name not in types:
                        types.append(type_name)
    return types


def normalize_page_url(url: str) -> str:
    """One spelling per page, for crawl dedup and finding keys: lowercase
    scheme + host, default port dropped, empty path -> '/', fragment dropped.
    The query string is kept (it can select a different page), and only the
    ROOT gains a slash — '/about' and '/about/' may be distinct pages."""
    parsed_url = urlparse(url.strip())
    scheme = parsed_url.scheme.lower()
    host = (parsed_url.hostname or "").lower()
    if parsed_url.port and (scheme, parsed_url.port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{parsed_url.port}"
    return parsed_url._replace(scheme=scheme, netloc=host, path=parsed_url.path or "/", fragment="").geturl()


def extract_internal_links(base_url: str, html: str) -> list[str]:
    """Same-host hrefs, absolutized + normalized (normalize_page_url), deduped in order."""
    base_hostname = urlparse(normalize_page_url(base_url)).netloc
    seen_urls: set[str] = set()
    internal_links: list[str] = []
    for href in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"']", html, re.I):
        absolute_url = normalize_page_url(urljoin(base_url, href.strip()))
        parsed_url = urlparse(absolute_url)
        is_same_host_web_link = parsed_url.scheme in ("http", "https") and parsed_url.netloc == base_hostname
        if is_same_host_web_link and absolute_url not in seen_urls:
            seen_urls.add(absolute_url)
            internal_links.append(absolute_url)
    return internal_links


def finding_key(kind: str, detail: dict) -> tuple[str, str]:
    """Dedupe identity for the open-findings diff: kind + the page it's about
    (normalized, so 'https://site' and 'https://site/' are one page)."""
    url = (detail or {}).get("url", "")
    return (kind, normalize_page_url(url) if url else "")


##############################
# SITEMAP + SECTIONS + ROTATION (pure)
##############################

SECTION_MAIN_PAGES = "Main pages"
SECTION_PAGES = "Pages"
SECTION_BLOG_POSTS = "Blog posts"
SECTION_PRODUCTS = "Products"
SECTION_CATEGORIES = "Categories"
SECTION_COLLECTIONS = "Collections"
# never-checked pages in these sections go right after the homepage: a CMS's
# "Pages" are the site's own pages (services, about, contact), before posts
MAIN_SECTIONS = {SECTION_MAIN_PAGES, SECTION_PAGES}

# child-sitemap file names that say what they list (basename, lowercased,
# .xml/.gz stripped), first match wins. WordPress core, then Yoast/Rank Math,
# then Shopify; the two generic custom-type rules come last.
SITEMAP_FILE_SECTIONS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^wp-sitemap-posts-page-\d+$"), SECTION_PAGES),
    (re.compile(r"^wp-sitemap-posts-post-\d+$"), SECTION_BLOG_POSTS),
    (re.compile(r"^wp-sitemap-taxonomies-.+$"), SECTION_CATEGORIES),
    (re.compile(r"^page-sitemap\d*$"), SECTION_PAGES),
    (re.compile(r"^post-sitemap\d*$"), SECTION_BLOG_POSTS),
    (re.compile(r"^product-sitemap\d*$"), SECTION_PRODUCTS),
    (re.compile(r"^(product_cat|category|post_tag|product_tag)-sitemap\d*$"), SECTION_CATEGORIES),
    (re.compile(r"^sitemap_products_\d+$"), SECTION_PRODUCTS),
    (re.compile(r"^sitemap_pages_\d+$"), SECTION_PAGES),
    (re.compile(r"^sitemap_collections_\d+$"), SECTION_COLLECTIONS),
    (re.compile(r"^sitemap_blogs_\d+$"), SECTION_BLOG_POSTS),
)  # fmt: skip
WP_CUSTOM_TYPE_FILE = re.compile(r"^wp-sitemap-posts-(.+)-\d+$")  # wp-sitemap-posts-service_area-1 -> "Service areas"
SEO_PLUGIN_TYPE_FILE = re.compile(r"^([a-z0-9_-]+?)-sitemap\d*$")  # service-sitemap.xml (Yoast/Rank Math custom type) -> "Services"
# names that match the generic rule without naming a page type
NOT_A_PAGE_TYPE = {"wp", "local", "geo", "video", "image", "news", "main", "default"}

# links the link crawl never treats as pages: files, admin, feeds
ASSET_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".zip", ".mp4", ".mp3", ".mov", ".css", ".js", ".xml", ".txt", ".json", ".doc", ".docx", ".xls", ".xlsx")
NON_PAGE_PATH_MARKERS = ("/wp-admin", "/wp-json", "/wp-login.php", "/xmlrpc.php", "/wp-content/", "/feed")


def parse_sitemap(body: str | None) -> tuple[str | None, list[str]]:
    """('index', child sitemap urls) | ('urlset', page urls) | (None, []).
    Takes each <sitemap>/<url> entry's own <loc> only — image/video
    extensions (<image:loc>) are never pages. CDATA + XML entities handled."""
    head = (body or "")[:4096]
    if re.search(r"<sitemapindex[\s>]", head, re.I):
        kind, entry = "index", r"<sitemap(?:\s[^>]*)?>(.*?)</sitemap>"
    elif re.search(r"<urlset[\s>]", head, re.I):
        kind, entry = "urlset", r"<url(?:\s[^>]*)?>(.*?)</url>"
    else:
        return None, []
    locs: list[str] = []
    for block in re.findall(entry, body or "", re.I | re.S):
        loc_match = re.search(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", block, re.I | re.S)
        if loc_match:
            loc = html_entities.unescape(loc_match.group(1)).strip()
            if loc and loc not in locs:
                locs.append(loc)
    return kind, locs


def _plain_words(text: str) -> str:
    return re.sub(r"\s+", " ", unquote(text).replace("-", " ").replace("_", " ")).strip().lower()


def _titleized(text: str) -> str:
    words = _plain_words(text)
    return words[:1].upper() + words[1:]


def _plural_title(type_name: str) -> str:
    """A post-type name as a section: 'service_area' -> 'Service areas'."""
    words = _plain_words(type_name)
    if not words.endswith("s"):
        words = words[:-1] + "ies" if re.search(r"[^aeiou]y$", words) else words + "s"
    return words[:1].upper() + words[1:]


def sitemap_section(sitemap_url: str) -> str | None:
    """The section a child sitemap's FILE NAME names, or None when it names
    none (a plain sitemap.xml) — then each page falls back to path_section."""
    name = (urlparse(sitemap_url).path or "").rsplit("/", 1)[-1].lower()
    name = re.sub(r"(\.xml)?(\.gz)?$", "", name)
    for pattern, section in SITEMAP_FILE_SECTIONS:
        if pattern.match(name):
            # the CMS "Pages" type is too vague a bar ("Pages" next to "Main
            # pages"): its pages are grouped by address instead — 'Service
            # areas', 'Services', 'Main pages' (the approved mockup)
            return None if section == SECTION_PAGES else section
    custom_type = WP_CUSTOM_TYPE_FILE.match(name) or SEO_PLUGIN_TYPE_FILE.match(name)
    if custom_type and custom_type.group(1) not in NOT_A_PAGE_TYPE:
        return _plural_title(custom_type.group(1))
    return None


def path_section(url: str) -> str:
    """Section from the URL alone: the homepage and single-segment top-level
    pages are 'Main pages'; deeper pages are named by their first segment
    ('/service-areas/tempe/' -> 'Service areas')."""
    segments = [segment for segment in (urlparse(url).path or "/").split("/") if segment]
    if len(segments) <= 1:
        return SECTION_MAIN_PAGES
    return _titleized(segments[0]) or SECTION_MAIN_PAGES


def _bare_host(netloc: str) -> str:
    return netloc[4:] if netloc.startswith("www.") else netloc


def same_site_url(url: str, base_url: str) -> str | None:
    """url on the brand's own site, normalized and moved onto the base URL's
    scheme + host (so a www/non-www or http/https spelling is one page); None
    for another host or a non-web link."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    candidate = urlparse(normalize_page_url(url))
    base = urlparse(normalize_page_url(base_url))
    if _bare_host(candidate.netloc) != _bare_host(base.netloc):
        return None
    return candidate._replace(scheme=base.scheme, netloc=base.netloc).geturl()


def is_page_like(url: str) -> bool:
    """Would the link crawl count this as a page? Not files, admin, feeds, or
    query-string variants (filters, pagination, add-to-cart)."""
    parsed = urlparse(url)
    path = (parsed.path or "/").lower()
    return not parsed.query and not path.endswith(ASSET_EXTENSIONS) and not any(marker in path for marker in NON_PAGE_PATH_MARKERS)


def page_links(page_url: str, base_url: str, html: str) -> list[str]:
    """The same-site, page-like links on a page (resolved against THAT page),
    normalized, deduped in order."""
    links: list[str] = []
    for href in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"']", html, re.I):
        url = same_site_url(urljoin(page_url, html_entities.unescape(href.strip())), base_url)
        if url and is_page_like(url) and url not in links:
            links.append(url)
    return links


async def read_sitemaps(roots: list[tuple[str, str]], base_url: str, fetch_body: Callable[[str], Awaitable[str | None]]) -> dict[str, str]:
    """Every page the site's sitemap(s) list -> its section. Walks sitemap
    indexes breadth-first (cycles skipped), same-site files and pages only,
    capped at MAX_SITEMAP_FILES documents read (roots included) and
    MAX_SITEMAP_URLS pages. fetch_body(url) -> sitemap text | None."""
    sections_by_url: dict[str, str] = {}
    queue = list(roots)
    visited = {url for url, _ in roots}
    files_read = len(roots)
    while queue:
        sitemap_url, body = queue.pop(0)
        kind, locs = parse_sitemap(body)
        if kind == "index":
            for loc in locs:
                if loc in visited or same_site_url(loc, base_url) is None:
                    continue
                if files_read >= MAX_SITEMAP_FILES or len(sections_by_url) >= MAX_SITEMAP_URLS:
                    break
                visited.add(loc)
                files_read += 1
                child_body = await fetch_body(loc)
                if child_body:
                    queue.append((loc, child_body))
        elif kind == "urlset":
            file_section = sitemap_section(sitemap_url)
            for loc in locs:
                page_url = same_site_url(loc, base_url)
                if page_url is None or page_url in sections_by_url:
                    continue
                if len(sections_by_url) >= MAX_SITEMAP_URLS:
                    return sections_by_url
                sections_by_url[page_url] = file_section or path_section(page_url)
    return sections_by_url


def page_status(detected_kinds: Iterable[str]) -> str:
    """A checked page's status from what THIS run detected on it (every
    detected finding is open once the diff ran): broken beats needs-fix
    beats slow; nothing -> healthy."""
    kinds = set(detected_kinds)
    if KIND_BROKEN_LINK in kinds:
        return STATUS_BROKEN
    if kinds & FIXABLE_KINDS:
        return STATUS_NEEDS_FIX
    if KIND_SLOW_PAGE in kinds:
        return STATUS_SLOW
    return STATUS_HEALTHY


@dataclass
class PageState:
    """One page as the rotation sees it (a site_pages row, or a page this run found)."""

    url: str
    section: str
    first_seen_at: datetime | None = None
    last_checked_at: datetime | None = None
    order: int = 0  # discovery order within a listing — keeps sitemap/crawl order among equals


def rotation_order(pages: dict[str, PageState], homepage_url: str, limit: int, exclude: Iterable[str] = ()) -> list[str]:
    """Which pages to check next: the homepage, then never-checked main pages,
    then other never-checked pages (oldest-known first, in listing order),
    then the longest-unchecked. Pure."""
    excluded = set(exclude)

    def sort_key(page: PageState) -> tuple:
        if page.url == homepage_url:
            return (0, datetime.min, 0, "")
        if page.last_checked_at is None:
            return (1 if page.section in MAIN_SECTIONS else 2, page.first_seen_at or datetime.min, page.order, page.url)
        return (3, page.last_checked_at, 0, page.url)

    return [page.url for page in heapq.nsmallest(limit, (page for url, page in pages.items() if url not in excluded), key=sort_key)]


def keys_to_resolve(open_finding_by_key: dict, detected_keys: set, checked_urls: set[str], probed_urls: set[str]) -> list:
    """Open finding keys this run proves gone: not re-detected AND actually
    looked at — site-wide kinds always (robots/sitemap are read every run), a
    page's findings only when that page was checked this run, and a broken
    link also when its URL was probed. Rotation means most pages were NOT
    checked: their findings stay open. Pure."""
    resolvable = []
    for key in open_finding_by_key:
        if key in detected_keys:
            continue
        kind, url = key
        if kind in SITE_WIDE_AUDIT_KINDS or url in checked_urls or (kind == KIND_BROKEN_LINK and url in probed_urls):
            resolvable.append(key)
    return resolvable


##############################
# CRAWL + DIFF
##############################


def split_duplicate_open_findings(open_findings: list) -> tuple[dict, list]:
    """Open findings by finding_key, oldest kept (it carries the issue's age);
    any later row under the same key is a duplicate for the diff to resolve."""
    open_finding_by_key: dict = {}
    duplicates: list = []
    for open_finding in sorted(open_findings, key=lambda finding: finding.created_at):
        key = finding_key(open_finding.kind, open_finding.detail)
        if key in open_finding_by_key:
            duplicates.append(open_finding)
        else:
            open_finding_by_key[key] = open_finding
    return open_finding_by_key, duplicates


@dataclass
class CrawlResult:
    """What one audit run saw: findings, the pages it vouched for (to upsert
    as current), and the pages it actually CHECKED (status per page) — the
    only pages whose open findings may auto-resolve."""

    findings: list[dict] = field(default_factory=list)
    seen: dict[str, str] = field(default_factory=dict)  # url -> section: listed / linked / carrying an open problem
    checks: list[tuple[str, str, str]] = field(default_factory=list)  # (url, section, page status), in check order
    probed: set[str] = field(default_factory=set)  # unlisted links fetched for broken-only detection
    from_sitemap: bool = False

    @property
    def checked_urls(self) -> set[str]:
        return {url for url, _, _ in self.checks}


def _page_state(url: str, section: str, known, default_first_seen: datetime, order: int) -> PageState:
    return PageState(url=url, section=section, first_seen_at=getattr(known, "first_seen_at", None) or default_first_seen, last_checked_at=getattr(known, "last_checked_at", None), order=order)


async def _crawl(base_url: str, known_pages: Iterable = (), problem_urls: Iterable[str] = ()) -> CrawlResult:
    """robots + sitemap (site-wide checks, every run), then the page list
    (sitemap; else the link crawl), then up to MAX_PAGES page checks in
    rotation order. known_pages = the brand's site_pages rows; problem_urls =
    pages with open findings (kept current until they resolve)."""
    result = CrawlResult()
    now = clock.now()
    homepage_url = normalize_page_url(base_url)
    known_by_url = {page.url: page for page in known_pages}
    cutoff = current_since()
    async with site_session(timeout=FETCH_TIMEOUT_SECONDS) as client:
        robots_url = urljoin(base_url, "/robots.txt")
        robots_txt = await _fetch_text(client, robots_url)
        for blocked_crawler in robots_blocked_agents(robots_txt or ""):
            result.findings.append(_finding(KIND_BLOCKED_AI_CRAWLER, SEVERITY_SERIOUS, f"robots.txt blocks {blocked_crawler} — invisible to that AI engine", robots_url, crawler=blocked_crawler))

        # keyed on the classic path so an existing open finding survives re-detection
        sitemap_url = urljoin(base_url, "/sitemap.xml")
        checked_sitemap_urls = sitemap_candidate_urls(base_url, robots_txt)
        sitemap_roots = await _find_sitemaps(client, checked_sitemap_urls, declared=robots_sitemap_urls(robots_txt or ""))
        if not sitemap_roots:
            result.findings.append(_finding(KIND_MISSING_SITEMAP, SEVERITY_WARN, f"No working sitemap found ({len(checked_sitemap_urls)} locations checked, including any robots.txt names)", sitemap_url, checked=checked_sitemap_urls))
        listed = await read_sitemaps(sitemap_roots, base_url, lambda url: _fetch_sitemap_body(client, url)) if sitemap_roots else {}
        result.from_sitemap = bool(listed)  # a sitemap listing nothing on this site -> link crawl

        # this run's vouched-for pages: the listing, the homepage, open problems
        result.seen.update(listed)
        result.seen.setdefault(homepage_url, getattr(known_by_url.get(homepage_url), "section", None) or SECTION_MAIN_PAGES)
        for problem_url in problem_urls:
            page_url = same_site_url(problem_url, base_url)
            if page_url and page_url not in result.seen:
                result.seen[page_url] = getattr(known_by_url.get(page_url), "section", None) or path_section(page_url)

        pages: dict[str, PageState] = {}
        for url, known in known_by_url.items():
            if known.last_seen_at and known.last_seen_at >= cutoff and url not in result.seen:
                pages[url] = _page_state(url, known.section, known, now, order=0)
        for order, (url, section) in enumerate(result.seen.items()):
            pages[url] = _page_state(url, section, known_by_url.get(url), now, order)

        unlisted_links: list[str] = []
        while len(result.checks) < MAX_PAGES:
            next_urls = rotation_order(pages, homepage_url, limit=1, exclude=result.checked_urls)
            if not next_urls:
                break
            page_url = next_urls[0]
            findings, html = await _check_page(client, page_url)
            result.findings.extend(findings)
            result.checks.append((page_url, pages[page_url].section, page_status(finding["kind"] for finding in findings)))
            for link in page_links(page_url, base_url, html or ""):
                if result.from_sitemap:
                    if link not in pages and link not in unlisted_links:
                        unlisted_links.append(link)  # linked but not listed: probed below, for broken links only
                    continue
                # link crawl: a linked page is a page (and is current while linked)
                if link not in result.seen and (link in pages or len(pages) < MAX_SITEMAP_URLS):
                    section = pages[link].section if link in pages else getattr(known_by_url.get(link), "section", None) or path_section(link)
                    result.seen[link] = section
                    if link not in pages:
                        pages[link] = _page_state(link, section, known_by_url.get(link), now, order=len(pages))

        # spare budget (small sites): probe unlisted internal links so a nav
        # link to a dead page still surfaces; a broken one is an open problem,
        # so it joins the page list (problem_urls) next run
        for link in unlisted_links[: max(0, MAX_PAGES - len(result.checks))]:
            result.probed.add(link)
            response = await _fetch(client, link, fresh=True)
            if response is None or response.status_code >= 400:
                result.findings.append(_broken_link_finding(link, response))
    return result


def _broken_link_finding(page_url: str, response) -> dict:
    status_label = str(response.status_code) if response is not None else "unreachable"
    return _finding(KIND_BROKEN_LINK, SEVERITY_SERIOUS, f"Internal link returns {status_label}: {page_url}", page_url, status=status_label)


async def _check_page(client: AsyncSession, page_url: str) -> tuple[list[dict], str | None]:
    """Fetch one page (live, cache-busted) and run the per-page rules.
    Returns (findings, html) — html None when broken or not a web page."""
    fetch_started_at = time.monotonic()
    page_response = await _fetch(client, page_url, fresh=True)
    elapsed_seconds = time.monotonic() - fetch_started_at
    if page_response is None or page_response.status_code >= 400:
        return [_broken_link_finding(page_url, page_response)], None
    if "text/html" not in page_response.headers.get("content-type", ""):
        return [], None
    return page_findings(page_url, page_response.text, elapsed_seconds), page_response.text


async def _find_sitemaps(client: AsyncSession, candidate_urls: list[str], declared: list[str] | None = None) -> list[tuple[str, str]]:
    """The sitemap roots to read, as (url, body): EVERY robots.txt-declared
    sitemap that serves sitemap XML, else the first conventional path that
    does. Empty = no working sitemap (the missing_sitemap finding)."""
    declared_urls = set(declared or [])
    roots: list[tuple[str, str]] = []
    for candidate_url in candidate_urls:
        if roots and candidate_url not in declared_urls:
            break
        body = await _fetch_sitemap_body(client, candidate_url)
        if body is not None:
            roots.append((candidate_url, body))
    return roots


async def _fetch_sitemap_body(client: AsyncSession, url: str) -> str | None:
    """A sitemap document's text (gzip files included), or None when the URL
    doesn't serve sitemap XML."""
    response = await _fetch(client, url, fresh=True)
    if response is None or response.status_code >= 400:
        return None
    body = body_text(response)
    return body if is_sitemap_document(response.headers.get("content-type", ""), body) else None


async def _fetch_text(client: AsyncSession, url: str) -> str | None:
    response = await _fetch(client, url, fresh=True)
    if response is None or response.status_code >= 400:
        return None
    return response.text


async def run_site_audit(brand_id: str, completing_task_id: str | None = None) -> dict:
    brand = await Brand.get(brand_id)
    site = await Site.by_brand(brand_id)

    # pages carrying an open problem stay on the page list until it resolves
    problem_urls = [str((finding.detail or {})["url"]) for finding in await Finding.open_for_brand(brand_id) if finding.kind in PAGE_KINDS and (finding.detail or {}).get("url")]
    crawl = await _crawl(brand.website_url, await SitePage.for_brand(brand_id), problem_urls)
    detected_findings = crawl.findings

    # re-read after the crawl: a fix that went live mid-crawl closed its finding
    # (worker/tasks/changes.py), and the reopen path below must see that
    open_findings = await Finding.open_for_brand(brand_id)
    open_finding_by_key, duplicate_open_findings = split_duplicate_open_findings([open_finding for open_finding in open_findings if open_finding.kind in AUDIT_KINDS])
    detected_finding_keys = {finding_key(detected["kind"], detected["detail"]) for detected in detected_findings}

    # issues closed the moment their fix went live (worker/tasks/changes.py):
    # one the crawl STILL sees is reopened with a "fix did not take" marker
    # rather than duplicated — a twin would chain a fresh fix every week
    fixed_by_change_by_key = {finding_key(fixed.kind, fixed.detail): fixed for fixed in await Finding.fixed_by_change_for_brand(brand_id) if fixed.kind in AUDIT_KINDS}

    created_count = 0
    reopened_count = 0
    new_fixable_finding_ids: list[str] = []
    for detected in detected_findings:
        detected_key = finding_key(detected["kind"], detected["detail"])
        already_open = detected_key in open_finding_by_key
        if already_open:
            existing = open_finding_by_key[detected_key]
            if plugin_unblocks_skip(existing.detail, detected["detail"]):
                await Finding.retry_after_skip(existing.id, detected["detail"].get("facts"))
                new_fixable_finding_ids.append(existing.id)
            continue  # keep the original row and its age
        if detected_key in fixed_by_change_by_key:
            fixed = fixed_by_change_by_key.pop(detected_key)
            await Finding.reopen_after_fix(fixed.id)
            reopened_count += 1
            continue
        finding = await Finding.create(brand_id=brand_id, site_id=site.id if site else None, kind=detected["kind"], severity=detected["severity"], summary=detected["summary"], detail=detected["detail"])
        created_count += 1
        if detected["kind"] in FIXABLE_KINDS:
            new_fixable_finding_ids.append(finding.id)
    if new_fixable_finding_ids:
        # #9 — the chained producer the schedules file points at: ONE task per
        # audit so agent mode reads the whole run at once (generators mode
        # loops the same ids)
        await create_task(brand_id=brand_id, name="technical_fix", queue="fixes", payload={"finding_ids": new_fixable_finding_ids})

    for duplicate_finding in duplicate_open_findings:
        await Finding.resolve(duplicate_finding.id, status=STATUS_DUPLICATE)  # a twin under one normalized key (pre-normalization 'https://site' vs 'https://site/'); not a fix (DEC-020)
    # gone AND looked at: a page outside this run's rotation keeps its open findings
    resolvable_keys = keys_to_resolve(open_finding_by_key, detected_finding_keys, crawl.checked_urls, crawl.probed)
    for key in resolvable_keys:
        await Finding.resolve(open_finding_by_key[key].id)
    resolved_count = len(resolvable_keys)

    checked_at = clock.now()
    await SitePage.upsert_seen(brand_id, crawl.seen, checked_at)
    await SitePage.record_checks(brand_id, crawl.checks, checked_at)

    logger.info(
        f"[audit] brand {brand_id}: {len(crawl.checks)} pages checked of {len(crawl.seen)} seen ({'sitemap' if crawl.from_sitemap else 'link crawl'}), {len(detected_findings)} detected, {created_count} new, {reopened_count} reopened (fix did not take), {resolved_count} auto-resolved, {len(duplicate_open_findings)} duplicates closed"
    )
    await queue_first_plan_when_ready(brand_id, completing_task_id)
    return {"detected": len(detected_findings), "created": created_count, "resolved": resolved_count, "checked": len(crawl.checks)}

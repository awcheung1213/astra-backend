"""The site audit (#3 ongoing): crawl -> audit rule engine -> typed findings.

Deterministic tier-1 code only — no LLM, $0. The rule engine's output is the
findings table (open -> fixed | dismissed):
- NEW fixable findings chain ONE technical_fix task per audit carrying their
  ids (#9 — this is the producer the schedules file points at); that task
  proposes fixes (generators or the Technical agent, per
  settings.TECHNICAL_FIX_MODE). Every finding also carries the page FACTS the
  crawler saw (title, metas, H1s, JSON-LD types, timing) so the fixer reads
  what the audit read instead of re-crawling.
- Open audit-managed findings that are NO LONGER detected auto-resolve
  (status fixed) — re-running the audit is idempotent, not additive.
- A finding a live fix closed (worker/tasks/changes.py) that the crawl still
  detects is REOPENED with a fix_did_not_take marker, never duplicated.

Crawling the client's PUBLIC site is a free, credential-less read, so it goes
through utils/site_fetch (browser-fingerprinted curl_cffi — see that module for
why not httpx) rather than an adapter — move it behind a crawler adapter when
rate limits or metering ever matter. The checks are pure functions over
fetched text (see tests/test_site_audit.py)."""

import json
import re
import time
from urllib.parse import urljoin, urlparse

from curl_cffi.requests import AsyncSession

from objects.brand import Brand
from objects.finding import SEVERITY_SERIOUS, SEVERITY_WARN, STATUS_DUPLICATE, Finding
from objects.site import Site
from utils.first_plan import queue_first_plan_when_ready
from utils.logger import logger
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
    findings dedupe, vanished ones auto-resolve — retries are safe."""
    async with task_lifecycle(task_id):
        await run_site_audit(brand_id, completing_task_id=task_id)


MAX_PAGES = 25
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


async def _crawl(base_url: str) -> list[dict]:
    """Fetch robots/sitemap/pages (capped) and run the rule engine."""
    findings: list[dict] = []
    async with site_session(timeout=FETCH_TIMEOUT_SECONDS) as client:
        robots_url = urljoin(base_url, "/robots.txt")
        robots_txt = await _fetch_text(client, robots_url)
        for blocked_crawler in robots_blocked_agents(robots_txt or ""):
            findings.append(_finding(KIND_BLOCKED_AI_CRAWLER, SEVERITY_SERIOUS, f"robots.txt blocks {blocked_crawler} — invisible to that AI engine", robots_url, crawler=blocked_crawler))

        # keyed on the classic path so an existing open finding survives re-detection
        sitemap_url = urljoin(base_url, "/sitemap.xml")
        checked_sitemap_urls = sitemap_candidate_urls(base_url, robots_txt)
        if not await _find_sitemap(client, checked_sitemap_urls):
            findings.append(_finding(KIND_MISSING_SITEMAP, SEVERITY_WARN, f"No working sitemap found ({len(checked_sitemap_urls)} locations checked, including any robots.txt names)", sitemap_url, checked=checked_sitemap_urls))

        urls_to_visit = [normalize_page_url(base_url)]
        visited_urls: set[str] = set()
        while urls_to_visit and len(visited_urls) < MAX_PAGES:
            page_url = urls_to_visit.pop(0)
            if page_url in visited_urls:
                continue
            visited_urls.add(page_url)

            fetch_started_at = time.monotonic()
            page_response = await _fetch(client, page_url)
            elapsed_seconds = time.monotonic() - fetch_started_at

            if page_response is None or page_response.status_code >= 400:
                status_label = str(page_response.status_code) if page_response is not None else "unreachable"
                findings.append(_finding(KIND_BROKEN_LINK, SEVERITY_SERIOUS, f"Internal link returns {status_label}: {page_url}", page_url, status=status_label))
                continue
            if "text/html" not in page_response.headers.get("content-type", ""):
                continue

            findings.extend(page_findings(page_url, page_response.text, elapsed_seconds))
            for internal_link in extract_internal_links(base_url, page_response.text):
                if internal_link not in visited_urls:
                    urls_to_visit.append(internal_link)
    return findings


async def _find_sitemap(client: AsyncSession, candidate_urls: list[str]) -> str | None:
    """First candidate that actually serves sitemap XML, else None."""
    for candidate_url in candidate_urls:
        response = await _fetch(client, candidate_url)
        if response is not None and response.status_code < 400 and is_sitemap_document(response.headers.get("content-type", ""), response.text):
            return candidate_url
    return None


async def _fetch_text(client: AsyncSession, url: str) -> str | None:
    response = await _fetch(client, url)
    if response is None or response.status_code >= 400:
        return None
    return response.text


async def run_site_audit(brand_id: str, completing_task_id: str | None = None) -> dict:
    brand = await Brand.get(brand_id)
    site = await Site.by_brand(brand_id)

    detected_findings = await _crawl(brand.website_url)

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
    resolved_count = 0
    for key, open_finding in open_finding_by_key.items():
        no_longer_detected = key not in detected_finding_keys
        if no_longer_detected:
            await Finding.resolve(open_finding.id)
            resolved_count += 1

    logger.info(f"[audit] brand {brand_id}: {len(detected_findings)} detected, {created_count} new, {reopened_count} reopened (fix did not take), {resolved_count} auto-resolved, {len(duplicate_open_findings)} duplicates closed")
    await queue_first_plan_when_ready(brand_id, completing_task_id)
    return {"detected": len(detected_findings), "created": created_count, "resolved": resolved_count}

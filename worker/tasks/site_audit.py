"""The site audit (#3 ongoing): crawl -> audit rule engine -> typed findings.

Deterministic tier-1 code only — no LLM, $0. The rule engine's output is the
findings table (open -> fixed | dismissed):
- NEW actionable findings chain a technical_fix task each (#9 — this is the
  producer the schedules file points at); the fix generators consume them and
  escalate odd cases to the Technical agent.
- Open audit-managed findings that are NO LONGER detected auto-resolve
  (status fixed) — re-running the audit is idempotent, not additive.

Crawling the client's PUBLIC site is a free, credential-less read, so it uses
httpx directly rather than an adapter — move it behind a crawler adapter when
rate limits or metering ever matter. The checks are pure functions over
fetched text (see tests/test_site_audit.py)."""


import re
import time
from urllib.parse import urljoin, urlparse

import httpx

from objects.brand import Brand
from objects.finding import SEVERITY_SERIOUS, SEVERITY_WARN, Finding
from objects.site import Site
from utils.logger import logger
from utils.task_queue import create_task
from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="site_audit", queue="audit", retry=2)
async def site_audit(task_id: str, brand_id: str) -> None:
    """#3 (ongoing) — crawl + audit rule engine over the brand's connected
    site -> typed findings. Fires at onboarding (site connect) and weekly via
    schedule_site_audits. Chains #9: each NEW actionable finding emits a
    technical_fix task (inside run_site_audit). Idempotent: re-detected
    findings dedupe, vanished ones auto-resolve — retries are safe."""
    async with task_lifecycle(task_id):
        await run_site_audit(brand_id)


MAX_PAGES = 25
FETCH_TIMEOUT_SECONDS = 10.0
SLOW_PAGE_SECONDS = 3.0

# the crawlers whose access decides AI visibility — blocking them is the
# single most self-defeating thing a client site can do
AI_CRAWLERS = ("GPTBot", "ClaudeBot", "Google-Extended", "PerplexityBot")

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


def _finding(kind: str, severity: str, summary: str, url: str, **extra_detail) -> dict:
    return {"kind": kind, "severity": severity, "summary": summary, "detail": {"url": url, **extra_detail}}


def page_findings(url: str, html: str, elapsed_seconds: float) -> list[dict]:
    """Deterministic per-page checks -> finding dicts (kind, severity, summary, detail)."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    has_title = bool(title_match and title_match.group(1).strip())
    # meta attributes appear in either order: name-then-content or content-then-name
    has_meta_description = bool(re.search(r"<meta[^>]+name=[\"']description[\"'][^>]+content=[\"'][^\"']+[\"']", html, re.I) or re.search(r"<meta[^>]+content=[\"'][^\"']+[\"'][^>]+name=[\"']description[\"']", html, re.I))
    has_h1 = bool(re.search(r"<h1[\s>]", html, re.I))
    has_schema_markup = bool(re.search(r"<script[^>]+application/ld\+json", html, re.I))
    is_slow = elapsed_seconds > SLOW_PAGE_SECONDS

    findings: list[dict] = []
    if not has_title:
        findings.append(_finding(KIND_MISSING_TITLE, SEVERITY_WARN, f"Page has no <title>: {url}", url))
    if not has_meta_description:
        findings.append(_finding(KIND_MISSING_META_DESCRIPTION, SEVERITY_WARN, f"Page has no meta description: {url}", url))
    if not has_h1:
        findings.append(_finding(KIND_MISSING_H1, SEVERITY_WARN, f"Page has no <h1>: {url}", url))
    if not has_schema_markup:
        findings.append(_finding(KIND_MISSING_SCHEMA, SEVERITY_WARN, f"Page has no structured data (ld+json): {url}", url))
    if is_slow:
        findings.append(_finding(KIND_SLOW_PAGE, SEVERITY_SERIOUS, f"Page took {elapsed_seconds:.1f}s to respond: {url}", url, elapsed_seconds=round(elapsed_seconds, 2)))
    return findings


def extract_internal_links(base_url: str, html: str) -> list[str]:
    """Same-host hrefs, absolutized, fragments stripped, deduped in order."""
    base_hostname = urlparse(base_url).netloc
    seen_urls: set[str] = set()
    internal_links: list[str] = []
    for href in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"']", html, re.I):
        absolute_url = urljoin(base_url, href.strip()).split("#", 1)[0]
        parsed_url = urlparse(absolute_url)
        is_same_host_web_link = parsed_url.scheme in ("http", "https") and parsed_url.netloc == base_hostname
        if is_same_host_web_link and absolute_url not in seen_urls:
            seen_urls.add(absolute_url)
            internal_links.append(absolute_url)
    return internal_links


def finding_key(kind: str, detail: dict) -> tuple[str, str]:
    """Dedupe identity for the open-findings diff: kind + the page it's about."""
    return (kind, (detail or {}).get("url", ""))


##############################
# CRAWL + DIFF
##############################


async def _crawl(base_url: str) -> list[dict]:
    """Fetch robots/sitemap/pages (capped) and run the rule engine."""
    findings: list[dict] = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": "EvergreenAudit/1.0"}) as client:
        robots_url = urljoin(base_url, "/robots.txt")
        robots_txt = await _fetch_text(client, robots_url)
        for blocked_crawler in robots_blocked_agents(robots_txt or ""):
            findings.append(_finding(KIND_BLOCKED_AI_CRAWLER, SEVERITY_SERIOUS, f"robots.txt blocks {blocked_crawler} — invisible to that AI engine", robots_url, crawler=blocked_crawler))

        sitemap_url = urljoin(base_url, "/sitemap.xml")
        sitemap_response = await _fetch(client, sitemap_url)
        if sitemap_response is None or sitemap_response.status_code >= 400:
            findings.append(_finding(KIND_MISSING_SITEMAP, SEVERITY_WARN, "No sitemap.xml", sitemap_url))

        urls_to_visit = [base_url]
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


async def _fetch(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    try:
        return await client.get(url)
    except httpx.HTTPError:
        return None


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    response = await _fetch(client, url)
    if response is None or response.status_code >= 400:
        return None
    return response.text


async def run_site_audit(brand_id: str) -> dict:
    """One brand's audit: crawl -> diff against open findings -> chain fixes.

    Returns counts for the runner's log line. Idempotent: re-detected findings
    are kept (not duplicated), vanished ones auto-resolve, and technical_fix
    tasks chain only off NEWLY created findings."""
    brand = await Brand.get(brand_id)
    site = await Site.by_brand(brand_id)

    detected_findings = await _crawl(brand.website_url)

    open_findings = await Finding.open_for_brand(brand_id)
    open_finding_by_key = {finding_key(open_finding.kind, open_finding.detail): open_finding for open_finding in open_findings if open_finding.kind in AUDIT_KINDS}
    detected_finding_keys = {finding_key(detected["kind"], detected["detail"]) for detected in detected_findings}

    created_count = 0
    for detected in detected_findings:
        already_open = finding_key(detected["kind"], detected["detail"]) in open_finding_by_key
        if already_open:
            continue  # keep the original row and its age
        finding = await Finding.create(brand_id=brand_id, site_id=site.id if site else None, kind=detected["kind"], severity=detected["severity"], summary=detected["summary"], detail=detected["detail"])
        created_count += 1
        if detected["kind"] in FIXABLE_KINDS:
            # #9 — the chained producer the schedules file points at
            await create_task(brand_id=brand_id, name="technical_fix", queue="fixes", payload={"finding_id": finding.id})

    resolved_count = 0
    for key, open_finding in open_finding_by_key.items():
        no_longer_detected = key not in detected_finding_keys
        if no_longer_detected:
            await Finding.resolve(open_finding.id)
            resolved_count += 1

    logger.info(f"[audit] brand {brand_id}: {len(detected_findings)} detected, {created_count} new, {resolved_count} auto-resolved")
    return {"detected": len(detected_findings), "created": created_count, "resolved": resolved_count}

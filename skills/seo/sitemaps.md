---
platforms: any
kinds: missing_sitemap
---
## Where sitemaps actually live

The audit tries robots.txt's `Sitemap:` line, then /sitemap.xml,
/sitemap_index.xml and /wp-sitemap.xml, and only flags `missing_sitemap` when
none serves sitemap XML with a 2xx. Before proposing anything, read the
finding's `checked` list. Common cases:

- WordPress core (5.5+) serves /wp-sitemap.xml automatically. If it answers
  404 with a valid `<sitemapindex>` body, the sitemap exists but a plugin or
  host rule is breaking its status code — search engines treat that as no
  sitemap. That is a hosting/plugin configuration fix, not a page edit: skip
  with a reason that names the exact URL and the status seen.
- Yoast and Rank Math serve /sitemap_index.xml and disable the core one.
- Shopify generates /sitemap.xml for every store; a missing one there means
  the store is password-protected or not yet published — skip with that reason.
- A sitemap is never created by editing page fields. There is no page
  operation for it; use `skip_finding` and say what the client or host must
  change.

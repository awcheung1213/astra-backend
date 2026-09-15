---
platforms: wordpress
kinds: any
---
## WordPress homepage and page resolution

- The homepage URL has no slug. `cms_get_page` on the bare site URL resolves
  it through Settings > Reading: a static front page (page_on_front) is an
  ordinary page you can edit; when the front page is the posts index there is
  no page object — nothing to write, so skip with that reason.
- Every other URL resolves by its last path segment, pages first, then posts.
- Native writable fields are `title`, `excerpt` and `content`. `h1` is an
  alias for `title` (themes render the page title as the H1) and
  `meta_description` is an alias for `excerpt` (core WordPress has no meta
  description field; most SEO plugins fall back to the excerpt).
- To add a JSON-LD block, read the page's current `content` and propose a
  `content` value that is the existing content plus one
  `<script type="application/ld+json">…</script>` block appended. Never
  replace content you have not read first.
- A page cached by LiteSpeed or a CDN may keep serving the old version for a
  while after a write; that is not a failed write.

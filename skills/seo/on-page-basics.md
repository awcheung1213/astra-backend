---
platforms: any
kinds: missing_title, missing_meta_description, missing_h1
---
## Titles, meta descriptions, H1s

- Title: the page's main heading or opening copy, then ` | Brand` unless the
  brand name is already in it. 50–60 characters is the sweet spot; never
  stuff keywords or repeat the brand twice.
- Meta description: 120–155 characters of plain customer-facing copy drawn
  from the page's own opening text — what the page offers and for whom. No
  internal facts, no claims the page does not make, no quotes.
- H1: one per page; the page title is the right source when the page has
  none. Do not invent a heading the page's content does not support.
- If the page's visible text is too thin to describe (under ~40 words), skip
  with that reason rather than fabricating copy.
- **WordPress without an SEO plugin cannot get a meta description from a page
  write.** Core WordPress has no meta-description field; the `excerpt` alias
  only becomes `<meta name="description">` when Yoast, Rank Math, AIOSEO,
  SEOPress or The SEO Framework renders it. Check the finding's
  `facts.seo_plugin`: when it is null on a WordPress site, do NOT propose an
  excerpt write (it changes nothing the audit can see) — skip with the reason
  that an SEO plugin must be installed first, naming the page(s). Found in the
  2026-09-15 e2e: an applied excerpt left the live page without the tag.

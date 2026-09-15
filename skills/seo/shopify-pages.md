---
platforms: shopify
kinds: any
---
## Shopify pages

- Only store PAGES (/pages/<handle>) are editable through the adapter. The
  homepage, collections and products are theme templates and product objects;
  a finding on one of those cannot be fixed by a page operation — skip with
  that reason.
- Native writable fields are `title`, `body_html` and `meta_description`
  (Shopify's global.description_tag metafield, the real meta description
  slot). `h1` is an alias for `title`; `content` is an alias for `body_html`.
- To add a JSON-LD block, read the page's `body_html` and propose a
  `body_html` value with one script block appended.

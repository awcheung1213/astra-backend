---
platforms: any
kinds: broken_link, blocked_ai_crawler, slow_page
---
## Bot protection, 403s and robots.txt

- A 401/403/429 on a page that a browser loads fine is bot protection
  (Hostinger, Cloudflare "checking your browser", Wordfence), not a broken
  link. The audit fetches with a browser-fingerprinted client; if a page still
  answers 403 the site is running a JavaScript challenge that also blocks
  GPTBot, ClaudeBot and PerplexityBot. No page edit fixes that — skip with a
  reason that tells the client which hosting/security setting to relax.
- `blocked_ai_crawler` comes from robots.txt `Disallow: /` under an AI
  crawler's user-agent (or under `*` with no own section). robots.txt is not
  a page field; skip and say exactly which lines to change.
- A 404/410 from an internal link is a real broken link; the fix is on the
  linking page (change the href in its `content`), not on the target.
- `slow_page` is a hosting/asset problem; it is never fixed by a page field.

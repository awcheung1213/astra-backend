---
platforms: any
kinds: missing_schema
---
## Choosing the schema.org type

- Pick the type from the business model, never from the brand's footprint
  flag. A software or online-service company is an `Organization`; add a
  nested `SoftwareApplication` only when the product has a name of its own.
  A place customers visit (dentist, salon, contractor with a service area) is
  a `LocalBusiness` subtype (`Dentist`, `HairSalon`, `Plumber`, …).
- `LocalBusiness` requires a postal `address` to validate; without a trusted
  address (from the brand facts, never scraped or guessed) use `Organization`
  instead — an invalid block is worse than a plain one.
- Always include `name` and `url`; add `logo`, `sameAs` (social profiles),
  `telephone` and `email` only when the brand facts state them.
- The `description` is PUBLIC copy: one plain sentence a customer would read.
  Never reuse internal phrasing from interviews or analysis (revenue mix,
  "only offering", strategy notes).
- One block per page. If the page already carries a JSON-LD block of the same
  type, skip — the audit will resolve the finding on its next crawl.

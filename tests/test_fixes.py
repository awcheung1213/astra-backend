"""technical_fix — the pure halves: payload compatibility, proposal validation, prompt assembly."""

from types import SimpleNamespace

from worker.tasks.fixes import GENERIC_FIELDS, _agent_prompt, _finding_ids, display_errors, same_site, validate_operations


def test_payload_accepts_the_per_audit_list_and_the_legacy_single_id():
    assert _finding_ids({"finding_ids": ["a", "b"]}) == ["a", "b"]
    assert _finding_ids({"finding_id": "a"}) == ["a"]
    assert _finding_ids({}) == [] and _finding_ids(None) == []


def test_same_site_ignores_www_and_scheme():
    assert same_site("http://www.x.com/about/", "https://x.com")
    assert not same_site("https://other.com/", "https://x.com")
    assert not same_site("not a url", "https://x.com")


def test_validate_operations_refuses_empty_offsite_and_unwritable_fields():
    assert validate_operations("wordpress", "https://x.com", []) == ["at least one operation is required"]
    errors = validate_operations(
        "wordpress",
        "https://x.com",
        [
            {"field": "title", "url": "https://x.com/a/", "value": "ok"},
            {"field": "schema_markup", "url": "https://x.com/a/", "value": {"@type": "Organization"}},
            {"field": "content", "url": "https://other.com/a/", "value": "<p>x</p>"},
            {"field": "excerpt", "url": "https://x.com/a/", "value": ""},
        ],
    )
    assert errors == ["operation 1: wordpress has no writable field 'schema_markup'", "operation 2: https://other.com/a/ is not on the brand's site", "operation 3: field, url and a non-empty value are required"]
    assert validate_operations("shopify", "https://x.com", [{"field": "content", "url": "https://x.com/pages/a", "value": "x"}]) == []  # alias -> body_html


def test_validate_operations_without_a_site_allows_only_generic_fields():
    assert validate_operations(None, "https://x.com", [{"field": "meta_description", "url": "https://x.com/", "value": "d"}]) == []
    assert validate_operations(None, "https://x.com", [{"field": "body_html", "url": "https://x.com/", "value": "d"}]) == [f"operation 0: unknown field 'body_html' (no site connected; use one of {', '.join(GENERIC_FIELDS)})"]


def test_agent_prompt_lists_findings_with_facts_and_the_platform_playbooks():
    brand = SimpleNamespace(name="Evergreen", website_url="https://x.com", description="An AI SEO platform", category="software", footprint="national")
    findings = [SimpleNamespace(id="f1", kind="missing_schema", summary="Page has no structured data", detail={"url": "https://x.com/", "facts": {"title": "Home", "h1s": ["Welcome"]}})]
    adapter = SimpleNamespace(NATIVE_FIELDS=("title", "excerpt", "content"), FIELD_ALIASES={"h1": "title"})
    prompt = _agent_prompt(brand, findings, "wordpress", adapter)
    assert "id=f1 kind=missing_schema url=https://x.com/" in prompt
    assert '"h1s": ["Welcome"]' in prompt
    assert "Writable fields: title, excerpt, content" in prompt
    assert "Choosing the schema.org type" in prompt and "WordPress homepage" in prompt  # skills selected by kind + platform
    assert "Shopify pages" not in prompt


GOOD_DISPLAY = {
    "title": "Add a business card for search engines to your contact page",
    "changes": [
        {
            "verb": "Add",
            "what": "a business card for search engines and AI assistants",
            "why": "It tells Google and AI assistants who you are so they describe you correctly.",
            "page": "your contact page",
            "shows": "Contact Evergreen — Evergreen is an AI SEO platform that measures whether you show up.",
        }
    ],
}


def test_display_gate_accepts_owner_level_copy_and_lets_the_quote_say_anything():
    assert display_errors(GOOD_DISPLAY, 1) == []  # 'SEO' inside the verbatim quote is fine


def test_display_gate_refuses_technical_words_shape_and_length():
    jargon = {**GOOD_DISPLAY, "changes": [{**GOOD_DISPLAY["changes"][0], "what": "a JSON-LD schema block in the page HTML"}]}
    assert display_errors(jargon, 1) == ["technical words are not allowed on the owner's card: block, html, json-ld, schema — say it in everyday words"]
    assert "display.changes has 1 entries for 2 operations" in display_errors(GOOD_DISPLAY, 2)
    assert display_errors({"title": "x", "changes": []}, 1) == ["display.changes must list one entry per operation"]
    assert "display.changes[0].verb must be one of Add, Set, Update" in display_errors({"title": "t", "changes": [{"verb": "Install", "what": "w", "why": "y", "page": "p", "shows": "s"}]}, 1)
    assert "display.title must be 80 characters or fewer" in display_errors({**GOOD_DISPLAY, "title": "x" * 81}, 1)
    assert display_errors("nope", 1) == ["display must be an object"]

"""skills_for — playbook selection by platform + finding kinds (pure, file-based)."""

from pathlib import Path

from utils.agent_prompts import SKILLS_DIR, skills_for


def _skills_dir(tmp_path: Path) -> Path:
    (tmp_path / "wp.md").write_text("---\nplatforms: wordpress\nkinds: any\n---\nWP HOMEPAGE")
    (tmp_path / "schema.md").write_text("---\nplatforms: any\nkinds: missing_schema, missing_title\n---\nSCHEMA TYPES")
    (tmp_path / "shop.md").write_text("---\nplatforms: shopify\nkinds: any\n---\nSHOPIFY PAGES")
    (tmp_path / "README.md").write_text("no front matter — never selected")
    return tmp_path


def test_selects_by_platform_and_kind(tmp_path):
    text = skills_for("wordpress", {"missing_schema"}, skills_dir=_skills_dir(tmp_path))
    assert "WP HOMEPAGE" in text and "SCHEMA TYPES" in text
    assert "SHOPIFY PAGES" not in text and "no front matter" not in text


def test_unrelated_kinds_and_unknown_platform_select_only_any_any(tmp_path):
    assert skills_for(None, {"slow_page"}, skills_dir=_skills_dir(tmp_path)) == ""
    assert skills_for("shopify", {"slow_page"}, skills_dir=_skills_dir(tmp_path)) == "SHOPIFY PAGES"


def test_shipped_skills_parse_and_cover_the_fixable_kinds():
    for kind in ("missing_schema", "missing_title", "missing_meta_description", "missing_h1", "missing_sitemap"):
        assert skills_for("wordpress", {kind}, skills_dir=SKILLS_DIR)

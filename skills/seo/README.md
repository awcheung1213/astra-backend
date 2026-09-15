# SEO skills — the Technical agent's playbooks

One markdown file per topic. The runner (`worker/tasks/fixes.py`) selects
files by the brand's CMS platform and the finding kinds in the run
(`utils/agent_prompts.skills_for`) and appends their bodies to the TASK
prompt (never the cached system prompt). A nuance that used to be a code
branch belongs here as a paragraph — see agent-tools-plan.md, rule 3.

Front matter (both keys required; `any` matches everything):

    ---
    platforms: wordpress, shopify | any
    kinds: missing_schema, missing_sitemap | any
    ---

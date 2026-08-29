"""PLACEHOLDER — kickoff research (deliverable #4, and #5's bootstrap half;
see the deliverables index in README.md).

Not cron work: fired by onboarding at brand creation and re-fired by plan
updates, so no schedule exists for it (see schedules.py's bottom comment).
Tasks register here (and in worker/app.py import_paths) in the same change as
their producer — the onboarding flow (api/onboarding.py)."""

# TODO(#4) prompt_research — keyword + AI-prompt research -> the brand's target
#       query list: generated prompts land inactive for human sign-off,
#       activated on approval; re-fired when a plan update shifts priorities.
# TODO(#5) competitor bootstrap — one LLM call seeding the brand's competitor
#       list at creation; the CONTINUOUS half of #5 already lives in
#       collection.py's ~5% discovery sampling (nothing to build there).

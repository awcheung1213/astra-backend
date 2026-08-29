"""PLACEHOLDER — the onboarding flow (deliverables #1, #2, #7, and the kickoff
fan-out of #4, #5, #6, #8; see the deliverables index in README.md).

Not cron work: these fire ONCE per brand from API events (signup, site/GBP
connect), which is why they live here and not in worker/tasks/schedules.py.
Routers register in main.py when this is built."""

# TODO(#1) access provisioning — create the client-contribution task rows
#       (grant_access, upload_photos, answer_sme_interview,
#       verify_conversion_tracking): rows with NO queue job; the daily
#       client_chase (worker/tasks/briefs.py) nags them to completion.
# TODO(#2) kickoff interview — AM-conducted at brand creation -> brand context
#       fields (description/audience/differentiators) + interview_raw verbatim.
# TODO(#7) conversion tracking setup — GA4/call-tracking config + its client
#       verification task (event fires, human confirms; chased like #1).
# TODO(#4,#5,#6,#8) kickoff fan-out — on launch, create_task for
#       prompt_research + competitor bootstrap (worker/tasks/research.py),
#       the baseline visibility_run + report (#6), and the first
#       plan_update (#8).

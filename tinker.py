"""We insert the Task row manually and call the runner's .func directly so this
script is the ONLY executor: create_task() would also enqueue a queue job,
and the live worker container would pick that up and run the brief a second
time (double Anthropic spend)."""

import asyncio

from sqlalchemy import select

from api.onboarding import interview
from objects.brand import Brand
from objects.task import Task
from schemas.onboarding_schemas import InterviewRequest
from seed import TEST_BRAND_NAME, seed
from utils.database import Database, ReplicaDatabase
from worker.tasks.research import research_competitors, research_prompts
from api.approvals import review_artifact, revert_change_set
from api.ask import ask
from api.connections import connect_site, disconnect_site, google_authorize, google_disconnect, google_pick_property
from api.onboarding import launch
from api.prompts import signoff_prompts
from api.webhooks import review_webhook
from schemas.approval_schemas import ReviewRequest
from schemas.ask_schemas import AskRequest
from schemas.connection_schemas import PickPropertyRequest, SiteConnectRequest
from schemas.onboarding_schemas import LaunchRequest
from schemas.prompt_schemas import SignoffRequest
from schemas.webhook_schemas import ReviewWebhookRequest
from objects.artifact import Artifact
from objects.prompt import Prompt
from worker.tasks.changes import change_set_apply
from worker.tasks.collection import poll_batches, submit_prompt_execution_batch
from worker.tasks.content import content_calendar, content_draft, content_refresh
from worker.tasks.fixes import technical_fix, technical_judgment
from worker.tasks.metrics import alert_detection, metrics_ingestion
from worker.tasks.outreach import outreach_scout
from worker.tasks.planning import diagnosis, plan_update
from worker.tasks.reports import monthly_report
from worker.tasks.reputation import gbp_apply, gbp_management, review_asks, review_response
from worker.tasks.research import research_competitors, research_prompts
from worker.tasks.site_audit import site_audit
from worker.tasks.visibility import visibility_run


# # A. worker task with your own row (payload only where noted)
# task = Task(brand_id=brand.id, name="plan_update", payload={})
# session.add(task); await session.commit()
# await plan_update.func(task_id=task.id, brand_id=brand.id)

# # B. API handler called directly, then commit (middleware would normally commit)
# from api.onboarding import launch
# from schemas.onboarding_schemas import LaunchRequest
# result = await launch(brand.id, LaunchRequest(goal_metric="leads", goal_target=40, approval_preset="review_everything"))
# await Database.session.commit()

# # C. run a row a handler already created, instead of inserting a duplicate
# row = await Task.first_queued(brand.id, "plan_update")
# await plan_update.func(task_id=row.id, brand_id=brand.id)

# # D. the two collection tasks take no arguments
# await submit_prompt_execution_batch.func()
# await poll_batches.func()

INTERVIEW_ANSWERS = {
    "Who's the dream customer — the call you hope is on the line when the phone rings?": "Local business and e-commerce businesses looking to scale traffic and revenue through AI-powered SEO solutions",
    "Welcome aboard. First: in a sentence or two, what does the business do — and which service makes you the most money?": "We provide Evergreen, an end to end AI SEO as a Software as a Service. This generates all of our revenue",
    "Last one: give me your proof points — years in business, licenses or awards, guarantees you honor, the compliment customers repeat in reviews. These become the credibility we write into every page.": "Customers frequently praise the transparency of work and the results on par or even better than their previous SEO agencies",
}


async def tinker() -> None:
    # await seed()

    # # ---- step 1: interview (#2) ---------------------------------------    $
    # # Output:
    # #   brands              UPDATE 1 row: interview_raw, description, category, audience, differentiators
    # #   cost_events         INSERT 1 row: operation=interview_extraction (the Account Manager call)
    # #   tasks               INSERT 2 rows, status=queued: research_prompts, research_competitors
    # #                       UPDATE answer_sme_interview -> done, only if such a client row exists (seed makes none)
    # #   procrastinate_jobs  INSERT 2 rows, status=todo: one per task above (worker stopped -> they sit; run via .func, then delete)
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
    #         await interview(brand.id, InterviewRequest(answers=INTERVIEW_ANSWERS))
    #         await Database.session.commit()  # the middleware would commit in a request; here we own it
    #         print(f"1.  interview -> {await Brand.get(brand.id)}")

    # # ---- step 2: research_prompts (#4) — chained from interview --------   $
    # # Output:
    # #   prompts             INSERT N rows: text, category, is_active=false (awaiting sign-off)
    # #   cost_events         INSERT per agent turn, operation=prompt_research
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
    #         row = await Task.first_queued(brand.id, "research_prompts")
    # await research_prompts.func(task_id=row.id, brand_id=brand.id)

    # # ---- step 3: research_competitors (#5) — chained from interview ----   $
    # # Output:
    # #   competitors         UPSERT N rows on normalized_name (original spelling kept in name)
    # #   cost_events         INSERT per agent turn
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         row = await Task.first_queued(brand.id, "research_competitors")
    # await research_competitors.func(task_id=row.id, brand_id=brand.id)

    # # ---- step 4: launch (#1 #7 #8 #3) ----------------------------------
    # # Output:
    # #   goals               INSERT 1 row: metric, target
    # #   approval_settings   REPLACE the brand's rows per preset (review_everything = nothing auto)
    # #   tasks               INSERT 3 bare client rows, status=queued, NO job: grant_access, upload_photos, verify_conversion_tracking
    # #                       (answer_sme_interview skipped because interview_raw is set)
    # #   tasks               INSERT 2 rows, status=queued: plan_update, site_audit
    # #   procrastinate_jobs  INSERT 2 rows, status=todo (worker stopped -> run via .func, then delete)
    # #   brands              UPDATE 1 row: onboarded_at=now, approval_preset   <- flips GET /me onboarded=true
    # #   second call         409
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
    #         launched = await launch(brand.id, LaunchRequest(goal_metric="leads", goal_target=40, approval_preset="review_everything"))
    #         await Database.session.commit()
    # print(f"4.  launch -> client_tasks={launched.client_tasks} queued_work={launched.queued_work}")

    # # ---- step 5: plan_update (#8 first run) — chained from launch ------   $ ** MAY NEED TO MOVE THIS TO LATER STEP (result keeps saying no metrics, etc. bc adapters not set up yet) **
    # # Output:
    # #   artifacts           INSERT 1 row: type=plan, status=pending_review, content=workstreams, evidence=facts
    # #   notifications       INSERT 1 row: kind=update, "Your new roadmap is ready for review", link_ref=artifacts/{id}
    # #   cost_events         INSERT per agent turn
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
    #         row = await Task.first_queued(brand.id, "plan_update")
    # await plan_update.func(task_id=row.id, brand_id=brand.id)

    # # ---- step 6: site_audit (#3) — chained from launch -----------------   $0
    # # Output:
    # #   findings            INSERT 1 row per NEW detected issue (kind, severity, summary, detail.url); open ones kept, not duplicated
    # #   findings            UPDATE resolved_at on audit-managed open findings no longer detected
    # #   tasks               INSERT 1 row per actionable new finding: technical_fix, payload={"finding_id"}
    # #   procrastinate_jobs  INSERT 1 row per technical_fix
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
    #         row = await Task.first_queued(brand.id, "site_audit")
    # await site_audit.func(task_id=row.id, brand_id=brand.id)

    # # ---- step 7: technical_fix (#9) — chained from site_audit, one per finding   $0
    # # Output, one of two paths per finding:
    # #   a) mechanical fix:  artifacts INSERT type=change_proposal, content.operations=[{field,url,value}], evidence.derived_from,
    # #                       status=pending_review (review_everything) | auto_approved (balanced/hands_off)
    # #                       if auto_approved AND a connected site: change_sets INSERT 1 + tasks INSERT change_set_apply payload={"change_set_id"} + job
    # #   b) needs judgment:  tasks INSERT technical_judgment, payload={"finding_id"} + job
    # async with ReplicaDatabase().connection():
    #     async with Database().connection():
    #         brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
    #         rows = await Task.for_brand(brand.id, statuses=["queued"])
    #         fix_rows = [row for row in rows if row.name == "technical_fix"]
    # for row in fix_rows:
    #     await technical_fix.func(task_id=row.id, brand_id=brand.id)

    # ---- step 8: technical_judgment (#9) — chained from technical_fix ---   $
    # Output:
    #   artifacts           INSERT 1 row: type=change_proposal, content.markdown proposal, status per preset (same dispatch rule as 7a)
    #   cost_events         INSERT per agent turn
    async with ReplicaDatabase().connection():
        async with Database().connection():
            brand = (await Database.session.execute(select(Brand).where(Brand.name == TEST_BRAND_NAME))).scalars().first()
            rows = await Task.for_brand(brand.id, statuses=["queued"])
            judgment_rows = [row for row in rows if row.name == "technical_judgment"]
    for row in judgment_rows:
        await technical_judgment.func(task_id=row.id, brand_id=brand.id)

if __name__ == "__main__":
    asyncio.run(tinker())

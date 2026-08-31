"""The Reputation agent's cadences (deliverables #14, #15): weekly GBP
touches and the review-ask templates. Drafts land behind the tone gate;
nothing posts or sends from here — the approval endpoint hands approved
work to the GBP/notifier adapters when those land."""

from adapters.agents.claude_agent import ClaudeAgent
from adapters.gbp_adapter import GBPAdapter
from objects.artifact import STATUS_APPROVED, STATUS_AUTO_APPROVED, STATUS_PENDING_REVIEW, Artifact
from objects.brand import Brand
from objects.connection import Connection
from objects.notification import KIND_ALERT, Notification
from objects.review import Review
from utils.agent_json import parse_agent_json
from utils.agent_prompts import ROLE_REPUTATION
from utils.logger import logger
from utils.task_queue import task_lifecycle
from worker.app import app


@app.task(name="gbp_management", queue="reputation", retry=2)
async def gbp_management(task_id: str, brand_id: str) -> None:
    """#14 — weekly GBP touches: posts drafted in brand voice -> ONE
    gbp_touches artifact behind the tone gate; approval chains gbp_apply.
    Last week's posts ride the prompt so the feed never repeats itself.
    Q&A was cut 2026-08-30 — the GBP Q&A API was discontinued 2025-11-03.
    (Review REPLIES are not here — they arrive via the new-review webhook
    as review_response tasks.)"""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        previous_touches = await Artifact.latest(brand.id, type="gbp_touches")
        previous_posts = (previous_touches.content.get("posts") if previous_touches else None) or []

        agent_output = await ClaudeAgent().run_role(
            ROLE_REPUTATION,
            prompt=_touches_prompt(brand, previous_posts),
            tools=[],
            handlers={},
            operation="gbp_management",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        touches = parse_agent_json(agent_output)
        if not isinstance(touches, dict) or not isinstance(touches.get("posts"), list) or not touches["posts"] or not all(isinstance(post, str) and post.strip() for post in touches["posts"]):
            raise ValueError(f"gbp output is not a posts list: {agent_output[:200]}")

        await Artifact.create(task_id=task_id, brand_id=brand.id, type="gbp_touches", content={"posts": touches["posts"]})
        logger.info(f"[reputation] brand {brand.id}: {len(touches['posts'])} posts drafted for review")


@app.task(name="review_asks", queue="reputation", retry=2)
async def review_asks(task_id: str, brand_id: str) -> None:
    """#15 — review generation: the ASKS are ours; the reviews are
    structurally the client's customers to give. v1 maintains the brand's ask
    TEMPLATES (email + SMS): the weekly tick no-ops while a current template
    artifact exists (rejected ones regenerate), so the cadence stays cheap
    until the sending machinery lands."""
    async with task_lifecycle(task_id):
        brand = await Brand.get(brand_id)
        current_templates = await Artifact.latest(brand.id, type="review_ask_templates", statuses=[STATUS_PENDING_REVIEW, STATUS_APPROVED, STATUS_AUTO_APPROVED])
        if current_templates is not None:
            logger.info(f"[reputation] brand {brand.id}: ask templates current — nothing to do")
            return

        agent_output = await ClaudeAgent().run_role(
            ROLE_REPUTATION,
            prompt=_ask_templates_prompt(brand),
            tools=[],
            handlers={},
            operation="review_asks",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        templates = parse_agent_json(agent_output)
        email_valid = isinstance(templates, dict) and isinstance(templates.get("email"), dict) and templates["email"].get("subject") and templates["email"].get("body")
        if not email_valid or not (isinstance(templates.get("sms"), str) and templates["sms"].strip()):
            raise ValueError(f"review-ask output is not email+sms templates: {agent_output[:200]}")

        await Artifact.create(task_id=task_id, brand_id=brand.id, type="review_ask_templates", content=templates)
        # TODO(#15): actually ASKING needs two things — a customer source
        #       (orders/calls via the metrics store or CMS adapter) to pick
        #       eligible recent customers, and the notifier adapter to send.
        #       Until both land, this task only maintains the templates.
        logger.info(f"[reputation] brand {brand.id}: review-ask templates drafted for review")


@app.task(name="review_response", queue="reputation", retry=2)
async def review_response(task_id: str, brand_id: str) -> None:
    """#14 (the reply half) — CHAINED from the review webhook (payload carries
    review_id); fast reply is the point. Every review gets a drafted reply in
    brand voice -> reply artifact behind the gate (outward -> always-approve
    floor; posting rides the GBP adapter after approval). A policy-violating
    review ALSO gets a flag_request artifact — but the reply is drafted
    regardless: removal is slow and often declined, and the reply is what
    prospects read meanwhile. Negative reviews ring Pulse."""
    async with task_lifecycle(task_id) as task:
        review_id = (task.payload or {}).get("review_id", "")
        review = await Review.get(review_id)
        if review.reply_artifact_id is not None:
            logger.info(f"[reputation] review {review.id}: reply already drafted — nothing to do")
            return
        brand = await Brand.get(brand_id)

        agent_output = await ClaudeAgent().run_role(
            ROLE_REPUTATION,
            prompt=_reply_prompt(brand, review),
            tools=[],
            handlers={},
            operation="review_response",
            organization_id=brand.organization_id,
            brand_id=brand.id,
            task_id=task_id,
        )
        response = parse_agent_json(agent_output)
        if not isinstance(response, dict) or not (isinstance(response.get("reply"), str) and response["reply"].strip()):
            raise ValueError(f"review response output has no reply: {agent_output[:200]}")

        reply_artifact = await Artifact.create(task_id=task_id, brand_id=brand.id, type="reply", content={"review_id": review.id, "platform": review.platform, "reply": response["reply"]}, evidence={"rating": review.rating, "author": review.author, "review_text": review.text})
        await Review.link_reply(review.id, reply_artifact.id)

        policy_violation = response.get("policy_violation")
        if isinstance(policy_violation, dict) and policy_violation.get("clause"):
            await Artifact.create(task_id=task_id, brand_id=brand.id, type="flag_request", content={"review_id": review.id, "platform": review.platform, "external_id": review.external_id, **policy_violation})

        if review.rating <= NEGATIVE_RATING_MAX:
            await Notification.create(organization_id=brand.organization_id, kind=KIND_ALERT, title="A negative review needs your attention", body=f"{review.rating}-star on {review.platform} — a reply is drafted for your approval.", link_ref=f"artifacts/{reply_artifact.id}")


@app.task(name="gbp_apply", queue="reputation", retry=2)
async def gbp_apply(task_id: str, brand_id: str) -> None:
    """CHAINED from the approval endpoint (payload carries artifact_id): puts
    an APPROVED gbp_touches or reply artifact live via the GBP adapter.

    Idempotency: a reply is a PUT (retry-safe) and guarded by replied_at;
    posts are NOT idempotent at the API — the double-post window is the gap
    between a post's 201 and this task's done-commit, accepted as rare."""
    async with task_lifecycle(task_id) as task:
        artifact = await Artifact.get((task.payload or {}).get("artifact_id", ""))
        if artifact.status != STATUS_APPROVED:
            logger.info(f"[reputation] artifact {artifact.id} is {artifact.status} — nothing to apply")
            return
        gbp_connection = await _gbp_connection(brand_id)
        if gbp_connection is None:
            logger.info(f"[reputation] artifact {artifact.id} approved but no GBP connection — apply by hand")
            return
        gbp = GBPAdapter(gbp_connection)

        if artifact.type == "gbp_touches":
            for post in artifact.content.get("posts", []):
                await gbp.create_post(post)
            logger.info(f"[reputation] artifact {artifact.id}: {len(artifact.content.get('posts', []))} posts live")
            return

        if artifact.type == "reply":
            review = await Review.get(artifact.content["review_id"])
            if review.replied_at is not None:
                logger.info(f"[reputation] review {review.id}: reply already live — nothing to do")
                return
            await gbp.reply_to_review(review.external_id, artifact.content["reply"])
            await Review.mark_replied(review.id)
            logger.info(f"[reputation] review {review.id}: reply live")
            return

        raise ValueError(f"gbp_apply cannot apply artifact type {artifact.type!r}")


async def _gbp_connection(brand_id: str) -> Connection | None:
    connections = await Connection.connected_for_brand(brand_id)
    return next((connection for connection in connections if connection.provider == "gbp"), None)


GBP_POST_COUNT = 2
NEGATIVE_RATING_MAX = 2  # at or below this, the owner hears about it directly


##############################
# TASK PROMPTS
##############################


def _touches_prompt(brand: Brand, previous_posts: list[str]) -> str:
    previous_lines = "\n".join(f"- {post}" for post in previous_posts) or "(none yet)"
    return (
        f"Draft this week's Google Business Profile posts: {GBP_POST_COUNT} of them. Posts are short, concrete,\n"
        "and about something true from the brand context — never invent events, offers, or claims.\n"
        "Do not repeat last week's posts.\n\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"posts": ["post body", ...]}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}\n"
        f"Differentiators: {brand.differentiators}\n\n"
        f"Last week's posts:\n{previous_lines}"
    )


def _reply_prompt(brand: Brand, review: Review) -> str:
    return (
        "Draft the brand's public reply to the customer review below. Thank genuinely, address the\n"
        "specifics they raised (never a template that ignores what they said), keep it short, and never\n"
        "argue, admit legal liability, or offer compensation. For a negative review: acknowledge, take it\n"
        "offline gracefully, stay composed.\n\n"
        "Separately judge whether the review violates the platform's content policy (fake, conflict of\n"
        "interest, hate, off-topic) — flag it ONLY when a specific clause genuinely applies.\n\n"
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"reply": "...", "policy_violation": null or {"clause": "the specific policy clause", "reason": "..."}}\n\n'
        f"Brand: {brand.name}\n"
        f"Description: {brand.description}\n\n"
        f"Review ({review.platform}, {review.rating} stars, by {review.author or 'anonymous'}):\n{review.text or '(no text — rating only)'}"
    )


def _ask_templates_prompt(brand: Brand) -> str:
    return (
        "Draft the brand's review-ask templates: one email (subject + body) and one SMS, sent to a customer\n"
        "shortly after a completed purchase or visit. Short, genuine, in the brand's voice. HARD RULES:\n"
        "never offer incentives for reviews and never filter by sentiment first (both violate Google's\n"
        'policy). Use "{first_name}" where the customer\'s name belongs and include the review link as\n'
        '"{review_link}".\n\n'
        "Respond with ONLY a JSON object, no prose and no code fences:\n"
        '{"email": {"subject": "...", "body": "..."}, "sms": "..."}\n\n'
        f"Brand: {brand.name} ({brand.website_url})\n"
        f"Category: {brand.category}\n"
        f"Description: {brand.description}\n"
        f"Audience: {brand.audience}"
    )

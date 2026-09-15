from schemas.base_schemas import BaseRequest, BaseResponse
from schemas.brand_schemas import ApprovalPreset


class SignupRequest(BaseRequest):
    organization_name: str
    brand_name: str
    website_url: str
    footprint: str | None = None  # local | national
    cities: list[str] | None = None  # "City, ST" strings; required when footprint is local
    goal_metric: str | None = None  # leads | revenue
    goal_target: float | None = None


class SignupResponse(BaseResponse):
    organization_id: str
    brand_id: str


class LaunchRequest(BaseRequest):
    """Launch-time preferences: the goal is chosen on the wizard's later
    pages, after signup (which fires when coverage is known) already ran."""

    goal_metric: str | None = None  # leads | revenue
    goal_target: float | None = None
    approval_preset: ApprovalPreset | None = None


class LaunchResponse(BaseResponse):
    brand_id: str
    client_tasks: list[str]  # #1/#7 — rows the client must complete (chased daily)
    queued_work: list[str]  # the kickoff fan-out actually enqueued


class InterviewRequest(BaseRequest):
    # question -> answer map from the AM-conducted interview; stored verbatim
    answers: dict


class InterviewResponse(BaseResponse):
    """The extraction for the client to confirm — extraction-filled,
    client-confirmed (edits ride the brand update endpoint when it lands)."""

    brand_id: str
    description: str
    category: str
    audience: str
    differentiators: list[str]

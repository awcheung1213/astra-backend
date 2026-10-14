"""Pure (no DB, no client fixture): outreach pitches that actually send —
route selection, MX classification, the scout's recipient guard, the
owner's edits, the send state machine (scheduled -> sending -> sent, cancel,
"I sent it", "Try again", never a double send, failure), message building
for all three routes, the consent URLs' scopes, the worklog receipt, and
create_task's delay. Every collaborator is a fake or monkeypatched."""

import base64
import email
import json
from datetime import datetime, timedelta
from email import policy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import api.connections as connections
import settings
import utils.approval_flow as approval_flow
import utils.task_queue as task_queue
import worker.tasks.outreach as outreach
from adapters import dns_adapter
from adapters.mail import outlook as outlook_mail
from adapters.mail import resend as resend_mail
from adapters.mail.common import MailSendError, build_rfc2822, jwt_claims_unverified
from adapters.mail.gmail import GMAIL_SEND_SCOPE, consented_mailbox
from utils import pitch_mail, receipts
from utils.pitch_mail import ROUTE_EVERGREEN, ROUTE_GMAIL, ROUTE_MANUAL, ROUTE_NONE, ROUTE_OUTLOOK, SEND_CANCELLED, SEND_FAILED, SEND_SCHEDULED, SEND_SENDING, SEND_SENT

NOW = datetime(2026, 10, 13, 12, 0)


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


##############################
# ROUTES + MX
##############################


def test_route_prefers_a_connected_mailbox_then_his_host_then_ours():
    select = pitch_mail.select_route
    assert select({"gmail", "outlook"}, None, True, True, True) == (ROUTE_GMAIL, True)
    assert select({"outlook"}, "google", True, True, True) == (ROUTE_OUTLOOK, True)
    assert select(set(), "google", True, True, True) == (ROUTE_GMAIL, False)
    assert select(set(), "microsoft", True, True, True) == (ROUTE_OUTLOOK, False)
    assert select(set(), "microsoft", True, False, True) == (ROUTE_EVERGREEN, True)  # MS app not configured
    assert select(set(), "google", False, True, True) == (ROUTE_EVERGREEN, True)  # Google app not configured
    assert select(set(), None, True, True, True) == (ROUTE_EVERGREEN, True)
    assert select(set(), None, True, True, False) == (ROUTE_NONE, False)


def test_mail_host_from_consumer_domain_or_mx():
    classify = pitch_mail.classify_mail_host
    assert classify("Marco@Gmail.com", None) == "google"
    assert classify("m@googlemail.com", None) == "google"
    for domain in ("outlook.com", "hotmail.com", "live.com", "msn.com"):
        assert classify(f"m@{domain}", None) == "microsoft"
    assert classify("m@reyesplumbing.com", ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]) == "google"
    assert classify("m@reyesplumbing.com", ["smtp.google.com."]) == "google"
    assert classify("m@reyesplumbing.com", ["reyesplumbing-com.mail.protection.outlook.com"]) == "microsoft"
    assert classify("m@reyesplumbing.com", ["mx1.privateemail.com"]) is None
    assert classify("m@reyesplumbing.com", None) is None  # lookup failed -> unknown
    assert classify("m@notgoogle.com", ["mail.evilgoogle.com"]) is None


def test_mx_answer_parsing_orders_by_preference():
    payload = {"Answer": [{"type": 15, "data": "10 alt1.aspmx.l.google.com."}, {"type": 15, "data": "1 ASPMX.L.GOOGLE.COM."}, {"type": 5, "data": "cname.example."}, {"type": 15, "data": "garbage"}]}
    assert dns_adapter.parse_mx_answer(payload) == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]
    assert dns_adapter.parse_mx_answer({}) == []


async def test_mx_lookup_failure_is_unknown_and_cached(monkeypatch):
    calls = []

    class Boom:
        def __init__(self, *args, **kwargs):
            calls.append(1)

        async def __aenter__(self):
            raise RuntimeError("no network")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(dns_adapter.httpx, "AsyncClient", Boom)
    monkeypatch.setattr(dns_adapter, "_mx_cache", {})
    assert await dns_adapter.mx_hosts("reyes-test.com") is None
    assert await dns_adapter.mx_hosts("reyes-test.com") is None
    assert len(calls) == 1


##############################
# RECIPIENTS (scout guard) + EDITS
##############################


def test_clean_recipient_keeps_real_addresses_and_drops_bad_ones():
    assert pitch_mail.clean_recipient(" mailto:Editor@AustinMonthly.com ") == "editor@austinmonthly.com"
    assert pitch_mail.clean_recipient("tips+local@news.co.uk") == "tips+local@news.co.uk"
    for bad in ("noreply@site.com", "no-reply@site.com", "donotreply@site.com", "postmaster@site.com", "hello@example.com", "you@yourdomain.com", "name@site.com", "logo@2x.png", "not an email", "a@b", "", None, "a..b@site.com", "x@site.invalid"):
        assert pitch_mail.clean_recipient(bad) is None, bad


def test_scout_pitch_keeps_only_addresses_seen_on_a_page():
    seen = outreach.normalize_pitch({"where": "https://austinmonthly.com/best-plumbers", "pitch": " Hi there ", "to_email": "Editor@AustinMonthly.com", "contact_source": "https://austinmonthly.com/contact", "subject": "Austin\nplumber   for your list"}, "Reyes Plumbing")
    assert seen["to_email"] == "editor@austinmonthly.com" and seen["contact_source"] == "https://austinmonthly.com/contact"
    assert seen["subject"] == "Austin plumber for your list" and seen["pitch"] == "Hi there"

    no_source = outreach.normalize_pitch({"where": "x", "pitch": "p", "to_email": "editor@austinmonthly.com"}, "Reyes Plumbing")
    assert no_source["to_email"] is None and no_source["contact_source"] is None and no_source["pitch"] == "p"
    bad = outreach.normalize_pitch({"where": "x", "pitch": "p", "to_email": "noreply@austinmonthly.com", "contact_source": "https://austinmonthly.com/"}, "Reyes Plumbing")
    assert bad["to_email"] is None and bad["subject"] == "A note from Reyes Plumbing"


def test_scout_caps_new_pitches_at_three():
    assert outreach.OPPORTUNITY_LIMIT == 3
    prompt = outreach._scout_prompt(SimpleNamespace(name="Reyes Plumbing", website_url="https://reyes.example", category="plumber", description="", audience="", differentiators=[], footprint="local"), "brief")
    assert "3 best opportunities" in prompt and "NEVER guess" in prompt and "under 120 words" in prompt and "under 8 words" in prompt


def test_edits_apply_only_sent_keys():
    content = {"where": "https://a.com", "pitch": "old", "subject": "old", "to_email": "a@a.com", "to_name": "Ann", "contact_source": "https://a.com/contact"}
    same_address = pitch_mail.apply_pitch_edits(content, {"subject": "New\r\nsubject", "body": " new body \n"})
    assert same_address["subject"] == "New subject" and same_address["pitch"] == "new body" and same_address["to_email"] == "a@a.com" and same_address["contact_source"] == "https://a.com/contact"
    typed = pitch_mail.apply_pitch_edits(content, {"to_email": "b@b.com", "to_name": None})
    assert typed["to_email"] == "b@b.com" and typed["to_name"] is None and typed["contact_source"] is None
    assert content["pitch"] == "old"  # never mutated
    assert pitch_mail.apply_pitch_edits(content, None) == content


##############################
# STATE MACHINE (pure)
##############################


def test_send_decision_sends_once_and_only_for_this_scheduling():
    send = pitch_mail.scheduled_send(ROUTE_GMAIL, "marco@gmail.com", "ed@a.com", NOW, "s1", "Subj", "Body")
    assert send["send_at"] == "2026-10-13T12:00:30+00:00" and send["subject"] == "Subj" and send["body"] == "Body"
    assert pitch_mail.send_decision("approved", send, "s1") == "send"
    assert pitch_mail.send_decision("approved", send, "s0") == "skip"  # stale job (unsend + re-approve)
    assert pitch_mail.send_decision("pending_review", send, "s1") == "skip"
    sending = pitch_mail.mark_sending(send, NOW)
    assert pitch_mail.send_decision("approved", sending, "s1") == "interrupted"
    sent = pitch_mail.mark_sent(sending, NOW, "m-1")
    assert sent["status"] == SEND_SENT and sent["open_url"] == "https://mail.google.com/mail/u/0/#sent" and sent["provider_message_id"] == "m-1"
    assert pitch_mail.send_decision("approved", sent, "s1") == "skip"
    assert pitch_mail.send_decision("approved", pitch_mail.mark_cancelled(send, NOW), "s1") == "skip"
    assert pitch_mail.mark_sent({"route": ROUTE_OUTLOOK}, NOW, None)["open_url"] == "https://outlook.live.com/mail/0/sentitems"
    assert pitch_mail.mark_sent({"route": ROUTE_EVERGREEN}, NOW, "r")["open_url"] is None
    assert pitch_mail.mark_failed(send, "nope", NOW)["status"] == SEND_FAILED


def test_unsend_retry_and_manual_rules():
    send = pitch_mail.scheduled_send(ROUTE_GMAIL, None, "ed@a.com", NOW, "s1")
    assert pitch_mail.can_unsend(send) and not pitch_mail.can_unsend(pitch_mail.mark_sending(send, NOW)) and not pitch_mail.can_unsend(None)
    failed = pitch_mail.mark_failed(send, "x", NOW)
    assert pitch_mail.can_review_pitch("pending_review", False, None)
    assert pitch_mail.can_review_pitch("approved", True, failed)  # "Try again"
    assert not pitch_mail.can_review_pitch("approved", False, failed)
    assert not pitch_mail.can_review_pitch("approved", True, send)
    assert not pitch_mail.can_review_pitch("approved", True, pitch_mail.mark_sent(send, NOW, None))
    assert pitch_mail.pitch_review_mode(False, None, ROUTE_NONE) == "manual"
    assert pitch_mail.pitch_review_mode(False, "ed@a.com", ROUTE_NONE) == "manual"
    assert pitch_mail.pitch_review_mode(False, None, ROUTE_GMAIL) == "manual"
    assert pitch_mail.pitch_review_mode(True, "ed@a.com", ROUTE_NONE) == "send"  # the caller 409s "not set up"
    assert pitch_mail.pitch_review_mode(True, None, ROUTE_GMAIL) == "send"  # the caller 422s "add their address"
    manual = pitch_mail.manual_send(NOW, "s2", "ed@a.com", "S", "B")
    assert (manual["status"], manual["route"], manual["sent_at"]) == (SEND_SENT, ROUTE_MANUAL, "2026-10-13T12:00:00+00:00")
    assert pitch_mail.sent_at({"send": manual}) == NOW and pitch_mail.sent_at({"send": send}) is None


##############################
# deliver_pitch (the task body, collaborators faked)
##############################


class _Store:
    def __init__(self, status, content):
        self.artifact = SimpleNamespace(id="a1", status=status, content=content)
        self.commits = 0
        self.history: list[str] = []
        self.notifications: list[dict] = []
        self.disconnected: list[str] = []


def _wire(monkeypatch, store: _Store, send_impl):
    async def get_for_update(artifact_id):
        return SimpleNamespace(id=store.artifact.id, status=store.artifact.status, content=dict(store.artifact.content))

    async def set_content(artifact_id, content, status=None):
        store.artifact.content = content
        if status is not None:
            store.artifact.status = status
        store.history.append(content["send"]["status"])

    async def commit():
        store.commits += 1

    async def brand_get(brand_id):
        return SimpleNamespace(id="b1", organization_id="o1", name="Reyes Plumbing")

    async def notify(**kwargs):
        store.notifications.append(kwargs)

    async def mark_disconnected(brand_id, provider):
        store.disconnected.append(provider)

    monkeypatch.setattr(outreach.Artifact, "get_for_update", get_for_update)
    monkeypatch.setattr(outreach.Artifact, "set_content", set_content)
    monkeypatch.setattr(outreach, "Database", SimpleNamespace(session=SimpleNamespace(commit=commit)))
    monkeypatch.setattr(outreach.Brand, "get", brand_get)
    monkeypatch.setattr(outreach.Notification, "create", notify)
    monkeypatch.setattr(outreach.Connection, "mark_disconnected", mark_disconnected)
    monkeypatch.setattr(outreach, "_send_via_route", send_impl)


def _pitch(send):
    return {"where": "https://austinmonthly.com/best", "pitch": "Hi", "subject": "Hello", "to_email": "ed@austinmonthly.com", "send": send}


async def test_deliver_marks_sending_before_the_call_then_sent(monkeypatch):
    store = _Store("approved", _pitch(pitch_mail.scheduled_send(ROUTE_GMAIL, "marco@gmail.com", "ed@austinmonthly.com", NOW, "s1")))
    calls = []

    async def send_impl(brand, content, send, task_id):
        calls.append(send["status"])
        assert store.commits == 1  # the `sending` marker was committed before the provider call
        return "gm-1"

    _wire(monkeypatch, store, send_impl)
    await outreach.deliver_pitch("a1", "s1", "b1", "t1")
    assert calls == [SEND_SENDING]
    assert store.history == [SEND_SENDING, SEND_SENT] and store.commits == 2
    assert store.artifact.content["send"]["provider_message_id"] == "gm-1" and store.artifact.content["send"]["sent_at"]

    # a retry/duplicate of the same job never sends again
    await outreach.deliver_pitch("a1", "s1", "b1", "t1")
    assert calls == [SEND_SENDING]


async def test_deliver_after_an_interrupted_attempt_never_sends(monkeypatch):
    store = _Store("approved", _pitch(pitch_mail.mark_sending(pitch_mail.scheduled_send(ROUTE_GMAIL, None, "ed@austinmonthly.com", NOW, "s1"), NOW)))

    async def send_impl(*args):
        raise AssertionError("must not send")

    _wire(monkeypatch, store, send_impl)
    await outreach.deliver_pitch("a1", "s1", "b1", "t1")
    assert store.artifact.content["send"]["status"] == SEND_FAILED and "Sent folder" in store.artifact.content["send"]["error"]
    assert store.artifact.status == "approved" and len(store.notifications) == 1


async def test_deliver_skips_cancelled_and_stale_jobs(monkeypatch):
    async def send_impl(*args):
        raise AssertionError("must not send")

    cancelled = _Store("pending_review", _pitch(pitch_mail.mark_cancelled(pitch_mail.scheduled_send(ROUTE_GMAIL, None, "ed@austinmonthly.com", NOW, "s1"), NOW)))
    _wire(monkeypatch, cancelled, send_impl)
    await outreach.deliver_pitch("a1", "s1", "b1", "t1")
    assert cancelled.history == [] and cancelled.artifact.content["send"]["status"] == SEND_CANCELLED

    rescheduled = _Store("approved", _pitch(pitch_mail.scheduled_send(ROUTE_GMAIL, None, "ed@austinmonthly.com", NOW, "s2")))
    _wire(monkeypatch, rescheduled, send_impl)
    await outreach.deliver_pitch("a1", "s1", "b1", "t1")  # the old job of an unsent-then-reapproved pitch
    assert rescheduled.history == [] and rescheduled.artifact.content["send"]["status"] == SEND_SCHEDULED


async def test_deliver_failure_records_plain_error_alerts_and_disconnects(monkeypatch):
    store = _Store("approved", _pitch(pitch_mail.scheduled_send(ROUTE_GMAIL, None, "ed@austinmonthly.com", NOW, "s1")))

    async def send_impl(*args):
        raise MailSendError("google token refresh -> 400", "Gmail didn't accept it — reconnect and try again.", reconnect=True)

    _wire(monkeypatch, store, send_impl)
    await outreach.deliver_pitch("a1", "s1", "b1", "t1")
    send = store.artifact.content["send"]
    assert send["status"] == SEND_FAILED and send["error"] == "Gmail didn't accept it — reconnect and try again."
    assert store.artifact.status == "approved"  # "Try again" re-approves it
    assert store.disconnected == ["gmail"]
    assert store.notifications[0]["title"] == "Your email to ed@austinmonthly.com didn't go out" and store.notifications[0]["link_ref"] == "artifacts/a1"


async def test_pitch_fan_out_defers_send_by_the_unsend_window(monkeypatch):
    created = []

    async def fake_create_task(**kwargs):
        created.append(kwargs)

    monkeypatch.setattr(approval_flow, "create_task", fake_create_task)
    artifact = SimpleNamespace(id="a1", brand_id="b1", type="pitch", content={"send": pitch_mail.scheduled_send(ROUTE_EVERGREEN, None, "ed@a.com", NOW, "s1")})
    assert await approval_flow.fan_out_approval(artifact) == ["send_pitch"]
    assert created == [{"brand_id": "b1", "name": "send_pitch", "queue": "outreach", "payload": {"artifact_id": "a1", "send_id": "s1"}, "delay_seconds": 30}]
    manual = SimpleNamespace(id="a2", brand_id="b1", type="pitch", content={"send": pitch_mail.manual_send(NOW, "s2", None)})
    assert await approval_flow.fan_out_approval(manual) == []
    assert "pitch" in approval_flow.ALWAYS_APPROVE_ARTIFACT_TYPES


async def test_create_task_passes_the_delay_to_the_same_session():
    executed = []

    class FakeSession:
        def add(self, row):
            row.id = "t-1"

        async def flush(self):
            return None

        async def execute(self, statement, params):
            executed.append((str(statement), params))

    await task_queue._create_task_and_job(FakeSession(), "b1", "send_pitch", {"artifact_id": "a1"}, "outreach", 0, None, None, None, 30)
    await task_queue._create_task_and_job(FakeSession(), "b1", "site_audit", None, "audit", 0, None, None, None)
    assert "interval '1 second'" in executed[0][0]
    assert executed[0][1]["delay_seconds"] == 30 and executed[1][1]["delay_seconds"] is None


##############################
# MESSAGES (three routes)
##############################


def test_rfc2822_is_plain_text_with_safe_headers():
    raw = build_rfc2822("Reyes Plumbing, Drain & Sewer", "marco@gmail.com", "ed@austinmonthly.com", "Ed\nSmith", "Hello\r\nBcc: evil@x.com", "Hi Ed,\n\nA plumber here.\n— Reyes Plumbing")
    message = email.message_from_bytes(raw, policy=policy.default)
    sender = message["From"].addresses[0]
    assert (sender.display_name, sender.addr_spec) == ("Reyes Plumbing, Drain & Sewer", "marco@gmail.com")  # the comma is quoted, not split
    assert message["To"] == "Ed Smith <ed@austinmonthly.com>"
    assert message["Subject"] == "Hello Bcc: evil@x.com" and message["Bcc"] is None
    assert message.get_content_type() == "text/plain" and "A plumber here." in message.get_content()
    assert b"\r\n" in raw and message["Reply-To"] is None and message["Cc"] is None


def test_graph_payload_is_plain_text_saved_to_sent_items():
    payload = outlook_mail.graph_send_payload("ed@austinmonthly.com", "Ed", "Hello", "Body")
    assert payload == {"message": {"subject": "Hello", "body": {"contentType": "Text", "content": "Body"}, "toRecipients": [{"emailAddress": {"address": "ed@austinmonthly.com", "name": "Ed"}}]}, "saveToSentItems": True}
    assert "name" not in outlook_mail.graph_send_payload("ed@a.com", None, "s", "b")["message"]["toRecipients"][0]["emailAddress"]


def test_resend_payload_never_puts_the_owner_in_from(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_FROM_DOMAIN", "mail.evergreen.test")
    payload = resend_mail.resend_payload("Reyes Plumbing", "ed@austinmonthly.com", "Ed", "Hello", "Body", "marco@reyesplumbing.com")
    assert payload["from"] == "Reyes Plumbing <outreach@mail.evergreen.test>"
    assert payload["reply_to"] == "marco@reyesplumbing.com" and payload["cc"] == ["marco@reyesplumbing.com"]
    assert payload["to"] == ["Ed <ed@austinmonthly.com>"] and payload["text"] == "Body" and "html" not in payload
    assert "marco@reyesplumbing.com" not in payload["from"]


##############################
# CONSENT URLS + CALLBACK HELPERS
##############################


def test_gmail_authorize_url_asks_send_only(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "cid")
    query = parse_qs(urlsplit(connections.google_authorize_url("gmail", "st", "marco@gmail.com")).query)
    assert set(query["scope"][0].split()) == {"openid", "email", GMAIL_SEND_SCOPE}
    assert (query["include_granted_scopes"], query["access_type"], query["prompt"], query["login_hint"]) == (["true"], ["offline"], ["consent"], ["marco@gmail.com"])
    assert "readonly" not in query["scope"][0] and "mail.google.com" not in query["scope"][0]
    gsc = parse_qs(urlsplit(connections.google_authorize_url("gsc", "st", "ignored@x.com")).query)
    assert "login_hint" not in gsc and "include_granted_scopes" not in gsc


def test_outlook_authorize_url_asks_send_only(monkeypatch):
    monkeypatch.setattr(settings, "MICROSOFT_OAUTH_CLIENT_ID", "cid")
    url = outlook_mail.outlook_authorize_url("st", "marco@outlook.com")
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    query = parse_qs(urlsplit(url).query)
    scopes = set(query["scope"][0].split())
    assert {"offline_access", "https://graph.microsoft.com/Mail.Send"} <= scopes
    assert not any("Read" in scope and "Mail" in scope for scope in scopes) and not any("ReadWrite" in scope for scope in scopes)
    assert query["login_hint"] == ["marco@outlook.com"] and query["state"] == ["st"]


def test_mail_return_urls_and_state_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_URL", "https://app.test")
    assert pitch_mail.mail_return_url("gmail", "connected") == "https://app.test/?screen=tasks&mail=gmail&result=connected"
    state = connections.mint_state("b1", "outlook")
    assert connections.verify_state(state)["provider"] == "outlook"
    assert connections.verify_state(state[:-1] + ("0" if state[-1] != "0" else "1")) is None


def test_consented_mailbox_requires_the_send_scope():
    tokens = {"scope": f"openid {GMAIL_SEND_SCOPE} https://www.googleapis.com/auth/userinfo.email", "id_token": _jwt({"email": "Marco@Gmail.com", "email_verified": True})}
    assert consented_mailbox(tokens) == "marco@gmail.com"
    assert consented_mailbox({**tokens, "scope": "openid email"}) is None  # unticked "send email on your behalf"
    assert consented_mailbox({**tokens, "id_token": _jwt({"email": "m@gmail.com", "email_verified": False})}) is None
    assert jwt_claims_unverified("not-a-jwt") == {}
    assert outlook_mail.email_from_microsoft_claims({"preferred_username": "Marco@Outlook.com"}) == "marco@outlook.com"
    assert outlook_mail.email_from_microsoft_claims({"preferred_username": "live.com#marco"}) is None


##############################
# WORKLOG RECEIPT
##############################


def test_sent_pitch_is_an_outreach_sent_outcome_dated_by_its_send():
    sent = pitch_mail.mark_sent(pitch_mail.scheduled_send(ROUTE_GMAIL, None, "ed@austinmonthly.com", NOW, "s1"), NOW + timedelta(days=40), "m")
    pitch = SimpleNamespace(id="p1", type="pitch", status="approved", content={"where": "https://www.austinmonthly.com/best", "send": sent}, evidence={}, created_at=NOW)
    outcome = receipts.artifact_outcome(pitch)
    assert outcome == {"id": "p1", "kind": "outreach_sent", "title": "Emailed austinmonthly.com", "when": NOW + timedelta(days=40), "ref": {"type": "artifact", "id": "p1"}}
    venue = SimpleNamespace(id="p2", type="pitch", status="approved", content={"where": "r/austin", "send": sent}, evidence={}, created_at=NOW)
    assert receipts.artifact_outcome(venue)["title"] == "Emailed austinmonthly.com"  # falls back to the recipient's domain
    unsent = SimpleNamespace(id="p3", type="pitch", status="pending_review", content={"where": "https://www.austinmonthly.com/best"}, evidence={}, created_at=NOW)
    assert receipts.artifact_outcome(unsent)["kind"] == "outreach"
    feed = receipts.done_for_you([outcome], since=NOW + timedelta(days=30))
    assert feed[0]["when"].endswith("+00:00")


@pytest.mark.parametrize("route", [ROUTE_GMAIL, ROUTE_OUTLOOK, ROUTE_EVERGREEN])
def test_every_route_has_an_open_url_entry(route):
    assert route in pitch_mail.OPEN_URLS

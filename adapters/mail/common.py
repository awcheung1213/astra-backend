"""What the three mail routes (gmail, outlook, evergreen/Resend) share: the
error the send task reads, the plain-text RFC 2822 builder, and the
unverified JWT-claims reader for an id_token received straight from a token
endpoint (over TLS, from the issuer itself — OIDC Core 3.1.3.7 lets a client
skip signature validation in exactly that case).

Bodies are PLAIN TEXT and sent as written: no tracking pixel, no link
rewriting, no HTML alternative."""

import base64
import json
import re
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, formatdate, make_msgid


class MailSendError(Exception):
    """A send did not (knowingly) go out.

    plain: the one line the owner reads. reconnect: the mailbox connection is
    dead (revoked/expired grant, missing scope) — mark it disconnected.
    maybe_sent: the request may have reached the provider (a timeout after
    sending) — the task must NOT offer an easy resend."""

    def __init__(self, message: str, plain: str, reconnect: bool = False, maybe_sent: bool = False) -> None:
        super().__init__(message)
        self.plain = plain
        self.reconnect = reconnect
        self.maybe_sent = maybe_sent


def header_safe(value: str | None) -> str:
    """One line: CR/LF/tabs collapsed (header injection can't ride a subject
    or a name). Pure."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def build_rfc2822(from_name: str, from_address: str, to_address: str, to_name: str | None, subject: str, body: str, reply_to: str | None = None, cc: str | None = None) -> bytes:
    """A plain-text UTF-8 message (CRLF line endings) ready for Gmail's
    users.messages.send `raw`. Pure."""
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr((header_safe(from_name), from_address))
    message["To"] = formataddr((header_safe(to_name), to_address)) if header_safe(to_name) else to_address
    if cc:
        message["Cc"] = cc
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = header_safe(subject)
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[-1] or None)
    message.set_content(body or "", subtype="plain", charset="utf-8")
    return message.as_bytes()


def base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def jwt_claims_unverified(token: str | None) -> dict:
    """The payload claims of a JWT, or {} when it is not one. Pure."""
    try:
        payload = str(token or "").split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}

"""Bounded, leased worker for SkillBridge transactional email delivery.

The worker is deliberately not started at import time. Call
``process_pending_emails_once`` from a separately managed worker process.
Only verification emails are currently implemented; other outbox purposes are
failed safely rather than being sent an incorrect template.

Delivery is at-least-once. Resend idempotency keys reduce duplicate submissions
within the provider's retention window, but no external provider and database
can participate in one atomic transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as URLRequest
from urllib.request import urlopen
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import EncryptionError, decrypt_token
from app.db.session import SessionLocal
from app.models.email_outbox import EmailOutbox
from app.models.verification_token import VerificationToken
from app.models.waitlist_entry import WaitlistEntry

logger = logging.getLogger(__name__)

# A claim counts as an attempt. A worker crash after claiming therefore also
# consumes an attempt when its lease is eventually recovered.
MAX_ATTEMPTS: Final[int] = 5
DEFAULT_BATCH_SIZE: Final[int] = 20
MAX_BATCH_SIZE: Final[int] = 50
DEFAULT_LEASE_SECONDS: Final[int] = 1_800
MIN_LEASE_SECONDS: Final[int] = 60
MAX_LEASE_SECONDS: Final[int] = 3_600
PROVIDER_TIMEOUT_SECONDS: Final[int] = 15
RETRY_BASE_SECONDS: Final[int] = 60
RETRY_MAX_SECONDS: Final[int] = 1_800
RESEND_API_URL: Final[str] = "https://api.resend.com/emails"
VERIFY_PATH: Final[str] = "/verify"

_TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,512}\Z")
_EMAIL_PATTERN = re.compile(r"\A[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\Z")


@dataclass(frozen=True, slots=True)
class _ClaimedEmail:
    """Detached data required to deliver one email after the claim commits."""

    outbox_id: UUID
    waitlist_id: UUID
    recipient: str | None
    purpose: str
    encrypted_token: str
    encryption_key_version: str
    attempt_count: int
    lock_owner: str


class _PermanentProcessingError(Exception):
    """A message/configuration error that must not be retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _TransientProcessingError(Exception):
    """A provider/network error that can be retried within the attempt limit."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_arguments(batch_size: int, lease_seconds: int, worker_id: str | None) -> str:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("batch_size must be an integer.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")

    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
        raise ValueError("lease_seconds must be an integer.")
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}."
        )

    if worker_id is None:
        prefix = "skillbridge-email-worker"
    else:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string when supplied.")
        prefix = worker_id.strip()
        if len(prefix) > 80 or any(ord(char) < 33 or ord(char) > 126 for char in prefix):
            raise ValueError("worker_id contains invalid characters or is too long.")

    # Every invocation gets a unique owner token, even when a stable worker name
    # is provided. A late worker cannot finalize a lease that another run reclaimed.
    return f"{prefix}:{uuid4()}"


def _secret_value(secret: object, setting_name: str) -> str:
    getter = getattr(secret, "get_secret_value", None)
    if not callable(getter):
        raise _PermanentProcessingError(f"invalid_{setting_name.lower()}")
    value = getter()
    if not isinstance(value, str) or not value.strip():
        raise _PermanentProcessingError(f"missing_{setting_name.lower()}")
    return value.strip()


def _build_verification_url(public_frontend_url: str, token: str) -> str:
    if not isinstance(public_frontend_url, str) or not public_frontend_url.strip():
        raise _PermanentProcessingError("missing_public_frontend_url")
    if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
        raise _PermanentProcessingError("invalid_decrypted_token")

    base_url = public_frontend_url.rstrip("/")
    return f"{base_url}{VERIFY_PATH}?{urlencode({'token': token})}"


def _build_email_content(verification_url: str) -> tuple[str, str, str]:
    subject = "Verify your SkillBridge waitlist entry"
    text_body = (
        "Welcome to SkillBridge!\n\n"
        "Please verify your email address by opening the link below:\n"
        f"{verification_url}\n\n"
        "If you did not request this, you can ignore this email."
    )
    safe_url = escape(verification_url, quote=True)
    html_body = (
        "<p>Welcome to SkillBridge!</p>"
        "<p>Please verify your email address by clicking the link below:</p>"
        f'<p><a href="{safe_url}">Verify email address</a></p>'
        "<p>If you did not request this, you can ignore this email.</p>"
    )
    return subject, text_body, html_body


def _validate_recipient(recipient: str | None) -> str:
    if not isinstance(recipient, str):
        raise _PermanentProcessingError("missing_recipient")
    normalized = recipient.strip()
    if (
        len(normalized) > 320
        or "\r" in normalized
        or "\n" in normalized
        or not _EMAIL_PATTERN.fullmatch(normalized)
    ):
        raise _PermanentProcessingError("invalid_recipient")
    return normalized


def _send_console(*, outbox_id: UUID, recipient: str, subject: str) -> None:
    """Development sink; deliberately never prints the verification link/token."""
    local, _, domain = recipient.partition("@")
    masked_recipient = f"{local[:2]}***@{domain}" if domain else "***"
    logger.info(
        "Console email sink accepted outbox_id=%s recipient=%s subject=%s "
        "verification_content=[REDACTED]",
        outbox_id,
        masked_recipient,
        subject,
    )


def _resend_error_code(error_body: bytes) -> str | None:
    """Extract only a non-sensitive provider error identifier, never its message."""
    try:
        decoded = json.loads(error_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    for key in ("name", "code", "error"):
        value = decoded.get(key)
        if isinstance(value, str) and value and len(value) <= 100:
            return value
    return None


def _send_resend(
    *,
    settings: object,
    outbox_id: UUID,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    api_key = _secret_value(getattr(settings, "RESEND_API_KEY", None), "RESEND_API_KEY")
    sender = getattr(settings, "EMAIL_FROM", None)
    if not isinstance(sender, str) or not sender.strip():
        raise _PermanentProcessingError("missing_email_from")
    if "\r" in sender or "\n" in sender:
        raise _PermanentProcessingError("invalid_email_from")

    payload = json.dumps(
        {
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "text": text_body,
            "html": html_body,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    request = URLRequest(
        RESEND_API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Resend's POST /emails idempotency keys are retained for 24 hours.
            # The UUID makes each outbox email a distinct provider operation.
            "Idempotency-Key": f"skillbridge-waitlist-verification-{outbox_id}",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=PROVIDER_TIMEOUT_SECONDS) as response:
            status_code = getattr(response, "status", None)
            response_body = response.read(64 * 1024)
    except HTTPError as exc:
        status_code = exc.code
        error_body = exc.read(8 * 1024)
        provider_code = _resend_error_code(error_body)
        if status_code == 409 and provider_code == "invalid_idempotent_request":
            raise _PermanentProcessingError("provider_idempotency_conflict") from None
        if (
            status_code in (408, 409, 425, 429)
            or (isinstance(status_code, int) and status_code >= 500)
        ):
            raise _TransientProcessingError(f"provider_http_{status_code}") from None
        raise _PermanentProcessingError(f"provider_http_{status_code}") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise _TransientProcessingError("provider_network_error") from None

    if not isinstance(status_code, int) or not 200 <= status_code < 300:
        raise _PermanentProcessingError("provider_unexpected_status")

    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # The provider may already have accepted the request. A retry with the
        # same deterministic idempotency key is safer than a new submission.
        raise _TransientProcessingError("provider_invalid_response") from None

    if not isinstance(decoded, dict) or not isinstance(decoded.get("id"), str) or not decoded["id"]:
        raise _TransientProcessingError("provider_invalid_response")


def _raise_smtp_response(status_code: int, phase: str) -> None:
    if 400 <= status_code < 500:
        raise _TransientProcessingError(f"smtp_{phase}_{status_code}") from None
    raise _PermanentProcessingError(f"smtp_{phase}_{status_code}") from None


def _send_smtp(
    *,
    settings: object,
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    username = getattr(settings, "SMTP_USERNAME", None)
    if not isinstance(username, str) or not username.strip():
        raise _PermanentProcessingError("missing_smtp_username")
    if "\r" in username or "\n" in username:
        raise _PermanentProcessingError("invalid_smtp_username")

    password = _secret_value(
        getattr(settings, "SMTP_PASSWORD", None),
        "SMTP_PASSWORD",
    )
    sender = getattr(settings, "EMAIL_FROM", None)
    if not isinstance(sender, str) or not sender.strip():
        raise _PermanentProcessingError("missing_email_from")
    if "\r" in sender or "\n" in sender:
        raise _PermanentProcessingError("invalid_email_from")

    _, sender_address = parseaddr(sender)
    if not sender_address:
        raise _PermanentProcessingError("invalid_email_from")

    host = getattr(settings, "SMTP_HOST", None)
    port = getattr(settings, "SMTP_PORT", None)
    if not isinstance(host, str) or not host.strip():
        raise _PermanentProcessingError("invalid_smtp_host")
    if isinstance(port, bool) or not isinstance(port, int) or port not in (587, 2525):
        raise _PermanentProcessingError("smtp_starttls_requires_port_587")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    client = None
    try:
        client = smtplib.SMTP(
            host=host.strip(),
            port=port,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )

        status_code, _ = client.ehlo()
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            _raise_smtp_response(
                status_code if isinstance(status_code, int) else 0,
                "ehlo",
            )

        client.starttls(context=ssl.create_default_context())

        status_code, _ = client.ehlo()
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            _raise_smtp_response(
                status_code if isinstance(status_code, int) else 0,
                "secure_ehlo",
            )

        client.login(username, password)
        refused = client.send_message(message)

        if refused:
            codes = [
                response[0]
                for response in refused.values()
                if isinstance(response, tuple)
                and response
                and isinstance(response[0], int)
            ]
            if codes and all(400 <= code < 500 for code in codes):
                raise _TransientProcessingError(
                    "smtp_recipient_temporary_rejection"
                ) from None
            raise _PermanentProcessingError(
                "smtp_recipient_rejected"
            ) from None

    except _TransientProcessingError:
        raise
    except _PermanentProcessingError:
        raise
    except smtplib.SMTPRecipientsRefused as exc:
        codes = [
            response[0]
            for response in exc.recipients.values()
            if isinstance(response, tuple)
            and response
            and isinstance(response[0], int)
        ]
        if codes and all(400 <= code < 500 for code in codes):
            raise _TransientProcessingError(
                "smtp_recipient_temporary_rejection"
            ) from None
        raise _PermanentProcessingError("smtp_recipient_rejected") from None
    except smtplib.SMTPResponseException as exc:
        _raise_smtp_response(exc.smtp_code, "server")
    except (
        smtplib.SMTPServerDisconnected,
        TimeoutError,
        socket.timeout,
        OSError,
    ):
        raise _TransientProcessingError("smtp_network_error") from None
    except smtplib.SMTPException:
        raise _PermanentProcessingError("smtp_protocol_error") from None
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass


def _validate_token_belongs_to_entry(message: _ClaimedEmail, raw_token: str) -> None:
    """Ensure the decrypted bearer token is the current token for this entry.

    The encryption AAD binds application/version/purpose, not the waitlist row.
    This additional lookup prevents an outbox payload from being accidentally
    swapped between recipients and avoids sending expired or consumed tokens.
    """
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now = _utc_now()
    db: Session = SessionLocal()
    try:
        token_id = db.scalar(
            select(VerificationToken.id)
            .join(WaitlistEntry, WaitlistEntry.id == VerificationToken.waitlist_id)
            .where(
                VerificationToken.waitlist_id == message.waitlist_id,
                WaitlistEntry.verified_at.is_(None),
                VerificationToken.token_hash == token_hash,
                VerificationToken.purpose == "verify",
                VerificationToken.consumed_at.is_(None),
                VerificationToken.expires_at > now,
            )
        )
        if token_id is None:
            raise _PermanentProcessingError("verification_token_not_current")
    finally:
        db.close()


def _deliver_one(settings: object, message: _ClaimedEmail) -> None:
    if message.purpose != "verify":
        raise _PermanentProcessingError("unsupported_email_purpose")

    recipient = _validate_recipient(message.recipient)

    envelope_version, separator, _ = message.encrypted_token.partition(".")
    if not separator or envelope_version != message.encryption_key_version:
        raise _PermanentProcessingError("encryption_version_mismatch")

    try:
        raw_token = decrypt_token(message.encrypted_token, "verify")
    except EncryptionError:
        raise _PermanentProcessingError("token_decryption_failed") from None

    _validate_token_belongs_to_entry(message, raw_token)

    public_url = getattr(settings, "PUBLIC_FRONTEND_URL", None)
    verification_url = _build_verification_url(public_url, raw_token)
    subject, text_body, html_body = _build_email_content(verification_url)

    mode = getattr(settings, "EMAIL_MODE", None)
    if mode == "console":
        _send_console(outbox_id=message.outbox_id, recipient=recipient, subject=subject)
        return
    if mode == "resend":
        _send_resend(
            settings=settings,
            outbox_id=message.outbox_id,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        return
    if mode == "smtp":
        _send_smtp(
            settings=settings,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        return

    raise _PermanentProcessingError("unsupported_email_mode")


def _retry_delay_seconds(
    attempt_count: int,
    *,
    base_seconds: int = RETRY_BASE_SECONDS,
) -> int:
    exponent = max(0, attempt_count - 1)
    return min(base_seconds * (2**exponent), RETRY_MAX_SECONDS)


def _clear_lease_values() -> dict[str, object]:
    return {
        "locked_at": None,
        "locked_until": None,
        "locked_by": None,
    }


def _claim_batch(
    *, batch_size: int, lease_seconds: int, lock_owner: str, summary: dict[str, int]
) -> list[_ClaimedEmail]:
    settings = get_settings()
    max_attempts = settings.OUTBOX_MAX_RETRIES
    now = _utc_now()
    stale_processing = and_(
        EmailOutbox.status == "PROCESSING",
        or_(EmailOutbox.locked_until.is_(None), EmailOutbox.locked_until <= now),
    )
    ready_pending = and_(
        EmailOutbox.status == "PENDING",
        EmailOutbox.available_at <= now,
    )

    statement = (
        select(EmailOutbox, WaitlistEntry.email)
        .outerjoin(WaitlistEntry, WaitlistEntry.id == EmailOutbox.waitlist_id)
        .where(or_(ready_pending, stale_processing))
        .order_by(EmailOutbox.available_at.asc(), EmailOutbox.created_at.asc())
        .limit(batch_size)
        .with_for_update(of=EmailOutbox, skip_locked=True)
    )

    db: Session = SessionLocal()
    claimed: list[_ClaimedEmail] = []
    exhausted_count = 0
    try:
        rows = db.execute(statement).all()
        for outbox, recipient in rows:
            current_attempts = outbox.attempt_count

            if current_attempts >= max_attempts:
                outbox.status = "FAILED"
                outbox.last_error = "maximum_attempts_exhausted_after_lease_expiry"
                for name, value in _clear_lease_values().items():
                    setattr(outbox, name, value)
                exhausted_count += 1
                continue

            outbox.status = "PROCESSING"
            outbox.attempt_count = current_attempts + 1
            outbox.locked_at = now
            outbox.locked_until = now + timedelta(seconds=lease_seconds)
            outbox.locked_by = lock_owner

            claimed.append(
                _ClaimedEmail(
                    outbox_id=outbox.id,
                    waitlist_id=outbox.waitlist_id,
                    recipient=recipient,
                    purpose=outbox.purpose,
                    encrypted_token=outbox.encrypted_token,
                    encryption_key_version=outbox.encryption_key_version,
                    attempt_count=current_attempts + 1,
                    lock_owner=lock_owner,
                )
            )

        db.commit()
        summary["claimed"] += len(claimed)
        summary["failed"] += exhausted_count
        return claimed
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error(
            "Email outbox claim transaction failed error_type=%s",
            type(exc).__name__,
        )
        summary["errors"] += 1
        return []
    finally:
        db.close()


def _finalize_success(message: _ClaimedEmail, summary: dict[str, int]) -> None:
    db: Session = SessionLocal()
    try:
        result = db.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.id == message.outbox_id,
                EmailOutbox.status == "PROCESSING",
                EmailOutbox.locked_by == message.lock_owner,
            )
            .values(
                status="SENT",
                sent_at=_utc_now(),
                last_error=None,
                **_clear_lease_values(),
            )
        )
        if result.rowcount != 1:
            db.rollback()
            summary["lease_lost"] += 1
            logger.warning(
                "Email outbox success finalization skipped after lease loss outbox_id=%s",
                message.outbox_id,
            )
            return
        db.commit()
        summary["sent"] += 1
    except SQLAlchemyError as exc:
        db.rollback()
        summary["errors"] += 1
        logger.error(
            "Email outbox success state could not be persisted outbox_id=%s error_type=%s",
            message.outbox_id,
            type(exc).__name__,
        )
    finally:
        db.close()


def _finalize_failure(
    message: _ClaimedEmail,
    summary: dict[str, int],
    *,
    error_code: str,
    retryable: bool,
) -> None:
    now = _utc_now()
    settings = get_settings()
    should_retry = (
        retryable
        and message.attempt_count < settings.OUTBOX_MAX_RETRIES
    )
    values: dict[str, object] = {
        "status": "PENDING" if should_retry else "FAILED",
        "last_error": error_code[:255],
        **_clear_lease_values(),
    }

    if should_retry:
        values["available_at"] = now + timedelta(
            seconds=_retry_delay_seconds(
                message.attempt_count,
                base_seconds=settings.OUTBOX_BACKOFF_BASE_SECONDS,
            )
        )

    db: Session = SessionLocal()
    try:
        result = db.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.id == message.outbox_id,
                EmailOutbox.status == "PROCESSING",
                EmailOutbox.locked_by == message.lock_owner,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            db.rollback()
            summary["lease_lost"] += 1
            logger.warning(
                "Email outbox failure finalization skipped after lease loss outbox_id=%s",
                message.outbox_id,
            )
            return

        db.commit()
        if should_retry:
            summary["retry_scheduled"] += 1
        else:
            summary["failed"] += 1
    except SQLAlchemyError as exc:
        db.rollback()
        summary["errors"] += 1
        logger.error(
            "Email outbox failure state could not be persisted outbox_id=%s error_type=%s",
            message.outbox_id,
            type(exc).__name__,
        )
    finally:
        db.close()


def process_pending_emails_once(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
) -> dict[str, int]:
    """Claim and process one bounded batch of due verification emails.

    The function performs no work during module import and contains no polling
    loop. It may be invoked periodically by a separately supervised worker.

    Returns counts only; it never returns recipient addresses, tokens, URLs,
    provider response bodies, or message contents.
    """
    settings = get_settings()
    effective_lease_seconds = (
        settings.OUTBOX_LEASE_SECONDS
        if lease_seconds is None
        else lease_seconds
    )
    lock_owner = _validate_arguments(
        batch_size, effective_lease_seconds, worker_id
    )
    summary = {
        "claimed": 0,
        "sent": 0,
        "retry_scheduled": 0,
        "failed": 0,
        "errors": 0,
        "lease_lost": 0,
    }

    claimed_messages = _claim_batch(
        batch_size=batch_size,
        lease_seconds=effective_lease_seconds,
        lock_owner=lock_owner,
        summary=summary,
    )

    for message in claimed_messages:
        try:
            _deliver_one(settings, message)
        except _PermanentProcessingError as exc:
            _finalize_failure(
                message,
                summary,
                error_code=exc.code,
                retryable=False,
            )
        except _TransientProcessingError as exc:
            _finalize_failure(
                message,
                summary,
                error_code=exc.code,
                retryable=True,
            )
        except Exception as exc:
            # Unexpected per-message errors are bounded retries, not silent
            # success. The stored/logged error contains only the exception type.
            logger.error(
                "Unexpected email outbox processing error outbox_id=%s error_type=%s",
                message.outbox_id,
                type(exc).__name__,
            )
            _finalize_failure(
                message,
                summary,
                error_code="unexpected_processing_error",
                retryable=True,
            )
        else:
            _finalize_success(message, summary)

    return summary

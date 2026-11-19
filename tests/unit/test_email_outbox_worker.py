from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.encryption import EncryptionError
from app.db.session import Base
from app.models.email_outbox import EmailOutbox
from app.models.verification_token import VerificationToken
from app.models.waitlist_entry import WaitlistEntry
from app.services import email_outbox_worker as worker


RAW_TOKEN = "A" * 43
ENCRYPTED_TOKEN = "v1.test-envelope"


class _Secret:
    def get_secret_value(self) -> str:
        return "re_test_key_only"


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


@pytest.fixture
def worker_environment(monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_char_length(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("char_length", 1, len)

    Base.metadata.create_all(
        engine,
        tables=[
            WaitlistEntry.__table__,
            VerificationToken.__table__,
            EmailOutbox.__table__,
        ],
    )
    test_session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", test_session_factory)

    settings = SimpleNamespace(
        EMAIL_MODE="console",
        EMAIL_FROM="SkillBridge <waitlist@example.org>",
        RESEND_API_KEY=None,
        SMTP_HOST="smtp.gmail.com",
        SMTP_PORT=587,
        SMTP_USERNAME=None,
        SMTP_PASSWORD=None,
        PUBLIC_FRONTEND_URL="https://skillbridge.example",
        OUTBOX_POLL_INTERVAL_SECONDS=5,
        OUTBOX_LEASE_SECONDS=1800,
        OUTBOX_MAX_RETRIES=5,
        OUTBOX_BACKOFF_BASE_SECONDS=60,
    )
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "decrypt_token", lambda envelope, purpose: RAW_TOKEN)

    yield SimpleNamespace(engine=engine, session_factory=test_session_factory, settings=settings)
    engine.dispose()


def _seed_message(
    factory,
    *,
    purpose: str = "verify",
    status: str = "PENDING",
    attempts: int = 0,
    locked_until: datetime | None = None,
    locked_by: str | None = None,
    token_value: str = RAW_TOKEN,
    consumed_at: datetime | None = None,
    expires_at: datetime | None = None,
    verified: bool = False,
) -> tuple[object, object]:
    now = datetime.now(timezone.utc)
    entry_id = uuid4()
    outbox_id = uuid4()
    with factory.begin() as db:
        db.add(
            WaitlistEntry(
                id=entry_id,
                email="student@example.org",
                normalized_email="student@example.org",
                consent_accepted_at=now,
                consent_version="consent-v1",
                reference_code=f"SB-{uuid4().hex[:24].upper()}",
                verified_at=now if verified else None,
            )
        )
        db.add(
            VerificationToken(
                id=uuid4(),
                waitlist_id=entry_id,
                token_hash=hashlib.sha256(token_value.encode("utf-8")).hexdigest(),
                purpose="verify",
                expires_at=expires_at or now + timedelta(hours=1),
                consumed_at=consumed_at,
            )
        )
        db.add(
            EmailOutbox(
                id=outbox_id,
                waitlist_id=entry_id,
                purpose=purpose,
                encrypted_token=ENCRYPTED_TOKEN,
                encryption_key_version="v1",
                status=status,
                attempt_count=attempts,
                available_at=now - timedelta(seconds=1),
                locked_at=now - timedelta(minutes=5) if status == "PROCESSING" else None,
                locked_until=locked_until,
                locked_by=locked_by,
            )
        )
    return entry_id, outbox_id


def _get_outbox(factory, outbox_id):
    with factory() as db:
        return db.get(EmailOutbox, outbox_id)


def test_console_delivery_marks_outbox_sent_and_clears_lease(worker_environment, caplog):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory)

    result = worker.process_pending_emails_once(worker_id="unit-test")

    record = _get_outbox(env.session_factory, outbox_id)
    assert result == {
        "claimed": 1,
        "sent": 1,
        "retry_scheduled": 0,
        "failed": 0,
        "errors": 0,
        "lease_lost": 0,
    }
    assert record.status == "SENT"
    assert record.attempt_count == 1
    assert record.sent_at is not None
    assert record.locked_at is None
    assert record.locked_until is None
    assert record.locked_by is None
    assert RAW_TOKEN not in caplog.text


def test_transient_failure_schedules_exponential_retry(worker_environment, monkeypatch):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory)

    def _transient_failure(**_kwargs):
        raise worker._TransientProcessingError("provider_network_error")

    monkeypatch.setattr(worker, "_send_console", _transient_failure)
    before = datetime.now(timezone.utc)
    result = worker.process_pending_emails_once()

    record = _get_outbox(env.session_factory, outbox_id)
    available_at = record.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=timezone.utc)
    assert result["retry_scheduled"] == 1
    assert record.status == "PENDING"
    assert record.attempt_count == 1
    assert record.last_error == "provider_network_error"
    assert available_at >= before + timedelta(seconds=worker.RETRY_BASE_SECONDS - 2)
    assert record.locked_by is None


def test_permanent_failure_is_not_retried(worker_environment):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory, purpose="delete")

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["failed"] == 1
    assert result["retry_scheduled"] == 0
    assert record.status == "FAILED"
    assert record.last_error == "unsupported_email_purpose"


def test_expired_lease_is_reclaimed_and_processed(worker_environment):
    env = worker_environment
    _, outbox_id = _seed_message(
        env.session_factory,
        status="PROCESSING",
        attempts=1,
        locked_by="dead-worker",
        locked_until=datetime.now(timezone.utc) - timedelta(seconds=10),
    )

    result = worker.process_pending_emails_once(worker_id="replacement-worker")
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["sent"] == 1
    assert record.status == "SENT"
    assert record.attempt_count == 2
    assert record.locked_by is None


def test_expired_lease_at_attempt_cap_is_failed(worker_environment):
    env = worker_environment
    _, outbox_id = _seed_message(
        env.session_factory,
        status="PROCESSING",
        attempts=worker.MAX_ATTEMPTS,
        locked_by="dead-worker",
        locked_until=datetime.now(timezone.utc) - timedelta(seconds=10),
    )

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["claimed"] == 0
    assert result["failed"] == 1
    assert record.status == "FAILED"
    assert record.last_error == "maximum_attempts_exhausted_after_lease_expiry"
    assert record.locked_by is None


def test_token_must_match_waitlist_entry(worker_environment):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory, token_value="different-token-value")

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["failed"] == 1
    assert record.status == "FAILED"
    assert record.last_error == "verification_token_not_current"


@pytest.mark.parametrize(
    "token_state",
    ["consumed", "expired"],
)
def test_consumed_or_expired_token_is_not_sent(worker_environment, token_state):
    env = worker_environment
    kwargs = {}
    if token_state == "consumed":
        kwargs["consumed_at"] = datetime.now(timezone.utc)
    else:
        kwargs["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    _, outbox_id = _seed_message(env.session_factory, **kwargs)

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["failed"] == 1
    assert record.status == "FAILED"
    assert record.last_error == "verification_token_not_current"


def test_old_worker_cannot_finalize_reclaimed_lease(worker_environment):
    env = worker_environment
    _, outbox_id = _seed_message(
        env.session_factory,
        status="PROCESSING",
        attempts=1,
        locked_by="current-owner",
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    with env.session_factory.begin() as db:
        record = db.get(EmailOutbox, outbox_id)
        waitlist_id = record.waitlist_id

    stale_message = worker._ClaimedEmail(
        outbox_id=outbox_id,
        waitlist_id=waitlist_id,
        recipient="student@example.org",
        purpose="verify",
        encrypted_token=ENCRYPTED_TOKEN,
        encryption_key_version="v1",
        attempt_count=1,
        lock_owner="old-owner",
    )
    summary = {"claimed": 0, "sent": 0, "retry_scheduled": 0, "failed": 0, "errors": 0, "lease_lost": 0}

    worker._finalize_success(stale_message, summary)
    record = _get_outbox(env.session_factory, outbox_id)

    assert summary["lease_lost"] == 1
    assert summary["sent"] == 0
    assert record.status == "PROCESSING"
    assert record.locked_by == "current-owner"


def test_invalid_arguments_are_rejected():
    with pytest.raises(ValueError):
        worker.process_pending_emails_once(batch_size=0)
    with pytest.raises(ValueError):
        worker.process_pending_emails_once(batch_size=True)
    with pytest.raises(ValueError):
        worker.process_pending_emails_once(lease_seconds=1)
    with pytest.raises(ValueError):
        worker.process_pending_emails_once(worker_id="bad\nworker")


def test_resend_success_uses_stable_idempotency_key(worker_environment, monkeypatch):
    env = worker_environment
    env.settings.EMAIL_MODE = "resend"
    env.settings.RESEND_API_KEY = _Secret()
    captured = {}

    def _fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(b'{"id":"email_test_123"}', status=200)

    monkeypatch.setattr(worker, "urlopen", _fake_urlopen)
    _, outbox_id = _seed_message(env.session_factory)

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)
    request = captured["request"]

    assert result["sent"] == 1
    assert record.status == "SENT"
    assert request.get_method() == "POST"
    # Authentication and request headers
    assert request.get_header("Authorization") == "Bearer re_test_key_only"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Idempotency-key") == f"skillbridge-waitlist-verification-{outbox_id}"
    assert captured["timeout"] == worker.PROVIDER_TIMEOUT_SECONDS

    # Email envelope and content
    body = json.loads(request.data.decode("utf-8"))
    assert body["from"] == "SkillBridge <waitlist@example.org>"
    assert body["to"] == ["student@example.org"]
    assert body["subject"] == "Verify your SkillBridge waitlist entry"

    # Verification link must be present in both email formats
    assert "https://skillbridge.example/verify?token=" in body["text"]
    assert "Verify email address" in body["html"]
    assert "<a href=" in body["html"]


def test_resend_rate_limit_is_retryable(worker_environment, monkeypatch):
    env = worker_environment
    env.settings.EMAIL_MODE = "resend"
    env.settings.RESEND_API_KEY = _Secret()

    def _raise_429(_request, timeout):
        raise HTTPError(
            worker.RESEND_API_URL,
            429,
            "rate limited",
            hdrs=None,
            fp=BytesIO(b'{"name":"rate_limit_exceeded"}'),
        )

    monkeypatch.setattr(worker, "urlopen", _raise_429)
    message = worker._ClaimedEmail(
        outbox_id=uuid4(),
        waitlist_id=uuid4(),
        recipient="student@example.org",
        purpose="verify",
        encrypted_token=ENCRYPTED_TOKEN,
        encryption_key_version="v1",
        attempt_count=1,
        lock_owner="owner",
    )
    with pytest.raises(worker._TransientProcessingError, match="provider_http_429"):
        worker._send_resend(
            settings=env.settings,
            outbox_id=message.outbox_id,
            recipient=message.recipient,
            subject="Verify",
            text_body="Text",
            html_body="<p>Text</p>",
        )


def test_resend_idempotency_payload_conflict_is_permanent(worker_environment, monkeypatch):
    env = worker_environment
    env.settings.RESEND_API_KEY = _Secret()

    def _raise_409(_request, timeout):
        raise HTTPError(
            worker.RESEND_API_URL,
            409,
            "conflict",
            hdrs=None,
            fp=BytesIO(b'{"name":"invalid_idempotent_request"}'),
        )

    monkeypatch.setattr(worker, "urlopen", _raise_409)
    with pytest.raises(worker._PermanentProcessingError, match="provider_idempotency_conflict"):
        worker._send_resend(
            settings=env.settings,
            outbox_id=uuid4(),
            recipient="student@example.org",
            subject="Verify",
            text_body="Text",
            html_body="<p>Text</p>",
        )


def test_bad_decryption_is_permanent(worker_environment, monkeypatch):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory)

    def _bad_decrypt(_envelope, _purpose):
        raise EncryptionError("do not expose cryptographic details")

    monkeypatch.setattr(worker, "decrypt_token", _bad_decrypt)
    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["failed"] == 1
    assert record.status == "FAILED"
    assert record.last_error == "token_decryption_failed"


def test_already_verified_entry_is_not_emailed(worker_environment):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory, verified=True)

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["failed"] == 1
    assert record.status == "FAILED"
    assert record.last_error == "verification_token_not_current"


def test_retry_attempt_cap_marks_message_failed(worker_environment, monkeypatch):
    env = worker_environment
    _, outbox_id = _seed_message(env.session_factory, attempts=worker.MAX_ATTEMPTS - 1)

    def _transient_failure(**_kwargs):
        raise worker._TransientProcessingError("provider_network_error")

    monkeypatch.setattr(worker, "_send_console", _transient_failure)
    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["failed"] == 1
    assert result["retry_scheduled"] == 0
    assert record.status == "FAILED"
    assert record.attempt_count == worker.MAX_ATTEMPTS
def _configure_smtp_settings(env):
    env.settings.EMAIL_MODE = "smtp"
    env.settings.EMAIL_FROM = "SkillBridge <team.skillbridgehq.in@gmail.com>"
    env.settings.SMTP_USERNAME = "team.skillbridgehq.in@gmail.com"
    env.settings.SMTP_PASSWORD = _Secret()
    env.settings.SMTP_HOST = "smtp.gmail.com"
    env.settings.SMTP_PORT = 587


def test_smtp_success_uses_starttls_and_sends_multipart_message(
    worker_environment, monkeypatch
):
    env = worker_environment
    _configure_smtp_settings(env)
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            captured["connection"] = (host, port, timeout)

        def ehlo(self):
            captured["ehlo_count"] = captured.get("ehlo_count", 0) + 1
            return 250, b"OK"

        def starttls(self, context):
            captured["tls_context"] = context

        def login(self, username, password):
            captured["login"] = (username, password)

        def send_message(self, message):
            captured["message"] = message
            return {}

        def quit(self):
            captured["quit"] = True

        def close(self):
            captured["close"] = True

    monkeypatch.setattr(worker.smtplib, "SMTP", FakeSMTP)

    worker._send_smtp(
        settings=env.settings,
        recipient="student@example.org",
        subject="Verify your SkillBridge waitlist entry",
        text_body="Verify at https://skillbridge.in/verify?token=test-token",
        html_body='<p><a href="https://skillbridge.in/verify?token=test-token">Verify</a></p>',
    )

    assert captured["connection"] == (
        "smtp.gmail.com", 587, worker.PROVIDER_TIMEOUT_SECONDS
    )
    assert captured["ehlo_count"] == 2
    assert captured["tls_context"] is not None
    assert captured["login"] == (
        "team.skillbridgehq.in@gmail.com", "re_test_key_only"
    )
    assert captured["quit"] is True

    message = captured["message"]
    assert message["From"] == (
        "SkillBridge <team.skillbridgehq.in@gmail.com>"
    )
    assert message["To"] == "student@example.org"
    assert message["Subject"] == "Verify your SkillBridge waitlist entry"
    assert message.get_content_type() == "multipart/alternative"
    assert "https://skillbridge.in/verify?token=test-token" in (
        message.get_body(preferencelist=("plain",)).get_content()
    )
    assert "Verify" in (
        message.get_body(preferencelist=("html",)).get_content()
    )


def test_smtp_temporary_network_failure_is_retryable(
    worker_environment, monkeypatch
):
    env = worker_environment
    _configure_smtp_settings(env)

    def fail_connection(**_kwargs):
        raise TimeoutError("private connection detail")

    monkeypatch.setattr(worker.smtplib, "SMTP", fail_connection)

    with pytest.raises(
        worker._TransientProcessingError, match="smtp_network_error"
    ) as exc:
        worker._send_smtp(
            settings=env.settings,
            recipient="student@example.org",
            subject="Verify",
            text_body="Text",
            html_body="<p>Text</p>",
        )

    assert "private connection detail" not in str(exc.value)


def test_smtp_authentication_failure_is_permanent_and_redacted(
    worker_environment, monkeypatch
):
    env = worker_environment
    _configure_smtp_settings(env)

    class FakeSMTP:
        def __init__(self, **_kwargs):
            pass

        def ehlo(self):
            return 250, b"OK"

        def starttls(self, context):
            pass

        def login(self, username, password):
            raise worker.smtplib.SMTPAuthenticationError(
                535, b"private authentication response"
            )

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(worker.smtplib, "SMTP", FakeSMTP)

    with pytest.raises(
        worker._PermanentProcessingError, match="smtp_server_535"
    ) as exc:
        worker._send_smtp(
            settings=env.settings,
            recipient="student@example.org",
            subject="Verify",
            text_body="Text",
            html_body="<p>Text</p>",
        )

    assert "private authentication response" not in str(exc.value)
    assert "re_test_key_only" not in str(exc.value)


def test_smtp_sender_must_match_authenticated_account(
    worker_environment, monkeypatch
):
    env = worker_environment
    _configure_smtp_settings(env)
    env.settings.EMAIL_FROM = "Other Sender <other@example.org>"

    def connection_must_not_start(**_kwargs):
        pytest.fail("SMTP connection must not start for a sender mismatch.")

    monkeypatch.setattr(worker.smtplib, "SMTP", connection_must_not_start)

    with pytest.raises(
        worker._PermanentProcessingError, match="smtp_sender_mismatch"
    ):
        worker._send_smtp(
            settings=env.settings,
            recipient="student@example.org",
            subject="Verify",
            text_body="Text",
            html_body="<p>Text</p>",
        )


def test_smtp_mode_dispatches_through_existing_outbox(
    worker_environment, monkeypatch
):
    env = worker_environment
    _configure_smtp_settings(env)
    captured = {}

    def fake_smtp_sender(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(worker, "_send_smtp", fake_smtp_sender)
    _, outbox_id = _seed_message(env.session_factory)

    result = worker.process_pending_emails_once()
    record = _get_outbox(env.session_factory, outbox_id)

    assert result["sent"] == 1
    assert record.status == "SENT"
    assert captured["recipient"] == "student@example.org"
    assert captured["settings"] is env.settings
    assert "skillbridge.example/verify?token=" in captured["text_body"]

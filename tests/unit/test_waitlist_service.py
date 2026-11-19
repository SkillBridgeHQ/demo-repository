import hashlib
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.email_outbox import EmailOutbox
from app.models.verification_token import VerificationToken
from app.models.vip_capacity import VipCapacity
from app.models.waitlist_entry import WaitlistEntry
from app.schemas.waitlist import (
    WaitlistRegisterRequest,
    WaitlistRegisterResponse,
)
from app.services import waitlist_service as service


RAW_TOKEN = "A" * 43
ENCRYPTED_ENVELOPE = "v1.test_nonce.test_ciphertext"


def configure_settings(monkeypatch, **overrides):
    values = {
        "CONSENT_VERSION": "test-consent-v1",
        "VERIFICATION_TOKEN_TTL_MINUTES": 1440,
        "ENCRYPTION_ACTIVE_KEY_VERSION": "v1",
        "EARLY_ACCESS_CAP": 500,
    }
    values.update(overrides)

    settings = SimpleNamespace(**values)

    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: settings,
    )

    return settings


def make_db():
    db = MagicMock(spec=Session)
    db.in_transaction.return_value = False
    db.begin.return_value = nullcontext()

    added_objects = []

    def capture_add(obj):
        added_objects.append(obj)

        # Simulate the UUID generated when SQLAlchemy inserts the entry.
        if isinstance(obj, WaitlistEntry) and obj.id is None:
            obj.id = uuid.uuid4()

    def capture_add_all(objects):
        added_objects.extend(objects)

    db.add.side_effect = capture_add
    db.add_all.side_effect = capture_add_all
    db.flush.return_value = None

    return db, added_objects


def make_registration_payload(
    email="Aryan@example.com",
    consent_version="test-consent-v1",
):
    return WaitlistRegisterRequest(
        email=email,
        consent_accepted=True,
        consent_version=consent_version,
    )


def make_verification_records(
    *,
    token=RAW_TOKEN,
    purpose="verify",
    expires_at=None,
    consumed_at=None,
    allocated_count=0,
):
    now = datetime.now(timezone.utc)
    entry_id = uuid.uuid4()

    token_record = VerificationToken(
        waitlist_id=entry_id,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        purpose=purpose,
        expires_at=expires_at or (now + timedelta(minutes=10)),
        consumed_at=consumed_at,
    )

    entry = WaitlistEntry(
        id=entry_id,
        email="aryan@example.com",
        normalized_email="aryan@example.com",
        consent_accepted_at=now,
        consent_version="test-consent-v1",
        reference_code="SB-TEST-123",
    )

    capacity = VipCapacity(
        id=1,
        allocated_count=allocated_count,
    )

    return token_record, entry, capacity


def make_integrity_error(constraint_name):
    original = SimpleNamespace(
        diag=SimpleNamespace(
            constraint_name=constraint_name,
        )
    )

    return IntegrityError(
        "INSERT",
        {},
        original,
    )


# ---------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------

def test_registration_stores_hash_and_encrypted_outbox(
    monkeypatch,
):
    configure_settings(monkeypatch)

    monkeypatch.setattr(
        service,
        "_generate_verification_token",
        lambda: RAW_TOKEN,
    )

    encrypt_mock = MagicMock(return_value=ENCRYPTED_ENVELOPE)

    monkeypatch.setattr(
        service,
        "encrypt_token",
        encrypt_mock,
    )

    db, added = make_db()

    response = service.register_waitlist_entry(
        db,
        make_registration_payload(),
    )

    entry = next(
        obj for obj in added
        if isinstance(obj, WaitlistEntry)
    )

    token_record = next(
        obj for obj in added
        if isinstance(obj, VerificationToken)
    )

    outbox_record = next(
        obj for obj in added
        if isinstance(obj, EmailOutbox)
    )

    expected_hash = hashlib.sha256(
        RAW_TOKEN.encode("utf-8")
    ).hexdigest()

    assert isinstance(response, WaitlistRegisterResponse)

    assert entry.id is not None
    assert entry.normalized_email == "aryan@example.com"
    assert entry.consent_version == "test-consent-v1"
    assert entry.reference_code.startswith("SB-")

    assert token_record.waitlist_id == entry.id
    assert token_record.token_hash == expected_hash
    assert token_record.purpose == "verify"
    assert RAW_TOKEN not in token_record.token_hash

    assert outbox_record.waitlist_id == entry.id
    assert outbox_record.purpose == "verify"
    assert outbox_record.encrypted_token == ENCRYPTED_ENVELOPE
    assert outbox_record.encryption_key_version == "v1"

    encrypt_mock.assert_called_once_with(RAW_TOKEN, "verify")

    assert db.begin.call_count == 1
    assert db.flush.call_count == 2


def test_duplicate_email_returns_generic_response(monkeypatch):
    configure_settings(monkeypatch)

    monkeypatch.setattr(
        service,
        "_generate_verification_token",
        lambda: RAW_TOKEN,
    )

    monkeypatch.setattr(
        service,
        "encrypt_token",
        lambda token, purpose: ENCRYPTED_ENVELOPE,
    )

    db, _ = make_db()

    db.flush.side_effect = make_integrity_error(
        "uq_waitlist_entries_normalized_email"
    )

    response = service.register_waitlist_entry(
        db,
        make_registration_payload(),
    )

    assert response == WaitlistRegisterResponse()
    db.add_all.assert_not_called()


def test_non_email_integrity_error_is_not_swallowed(monkeypatch):
    configure_settings(monkeypatch)

    monkeypatch.setattr(
        service,
        "_generate_verification_token",
        lambda: RAW_TOKEN,
    )

    monkeypatch.setattr(
        service,
        "encrypt_token",
        lambda token, purpose: ENCRYPTED_ENVELOPE,
    )

    db, _ = make_db()

    db.flush.side_effect = make_integrity_error(
        "uq_waitlist_entries_reference_code"
    )

    with pytest.raises(IntegrityError):
        service.register_waitlist_entry(
            db,
            make_registration_payload(),
        )


def test_registration_rejects_stale_consent_version(monkeypatch):
    configure_settings(monkeypatch)

    db, _ = make_db()

    with pytest.raises(service.ConsentVersionMismatchError):
        service.register_waitlist_entry(
            db,
            make_registration_payload(
                consent_version="old-consent-version",
            ),
        )

    db.begin.assert_not_called()
    db.add.assert_not_called()


def test_registration_rejects_missing_consent_configuration(
    monkeypatch,
):
    configure_settings(
        monkeypatch,
        CONSENT_VERSION=None,
    )

    db, _ = make_db()

    with pytest.raises(service.ConsentConfigurationError):
        service.register_waitlist_entry(
            db,
            make_registration_payload(),
        )

    db.begin.assert_not_called()


def test_service_rejects_session_with_active_transaction(
    monkeypatch,
):
    configure_settings(monkeypatch)

    db, _ = make_db()
    db.in_transaction.return_value = True

    with pytest.raises(service.TransactionBoundaryError):
        service.register_waitlist_entry(
            db,
            make_registration_payload(),
        )

    db.begin.assert_not_called()


# ---------------------------------------------------------------------
# Verification tests
# ---------------------------------------------------------------------

def test_verification_consumes_token_and_allocates_vip(
    monkeypatch,
):
    configure_settings(monkeypatch)

    token_record, entry, capacity = make_verification_records()

    db, _ = make_db()
    db.scalar.side_effect = [
        token_record,
        entry,
        capacity,
    ]

    response = service.verify_waitlist_email(
        db,
        RAW_TOKEN,
    )

    assert response.verified is True
    assert response.reference_code == "SB-TEST-123"
    assert response.vip_number == 1

    assert token_record.consumed_at is not None
    assert entry.verified_at is not None

    assert entry.vip_number == 1
    assert capacity.allocated_count == 1

    # Verify that each SELECT used for locking compiles to PostgreSQL
    # SQL containing FOR UPDATE.
    assert db.scalar.call_count == 3

    for call in db.scalar.call_args_list:
        statement = call.args[0]
        compiled_sql = str(
            statement.compile(dialect=postgresql.dialect())
        )

        assert "FOR UPDATE" in compiled_sql.upper()


def test_verification_does_not_allocate_when_capacity_is_full(
    monkeypatch,
):
    configure_settings(monkeypatch)

    token_record, entry, capacity = make_verification_records(
        allocated_count=500,
    )

    db, _ = make_db()
    db.scalar.side_effect = [
        token_record,
        entry,
        capacity,
    ]

    response = service.verify_waitlist_email(
        db,
        RAW_TOKEN,
    )

    assert response.verified is True
    assert response.reference_code == "SB-TEST-123"
    assert response.vip_number is None

    assert entry.verified_at is not None
    assert entry.vip_number is None
    assert capacity.allocated_count == 500
    assert token_record.consumed_at is not None


def test_unknown_token_is_rejected(monkeypatch):
    configure_settings(monkeypatch)

    db, _ = make_db()
    db.scalar.return_value = None

    with pytest.raises(service.InvalidVerificationTokenError):
        service.verify_waitlist_email(
            db,
            RAW_TOKEN,
        )

    assert db.scalar.call_count == 1


@pytest.mark.parametrize(
    "invalid_state",
    [
        "expired",
        "consumed",
        "wrong_purpose",
    ],
)
def test_invalid_token_state_is_rejected(
    monkeypatch,
    invalid_state,
):
    configure_settings(monkeypatch)

    now = datetime.now(timezone.utc)

    if invalid_state == "expired":
        token_record, _, _ = make_verification_records(
            expires_at=now - timedelta(minutes=1),
        )

    elif invalid_state == "consumed":
        token_record, _, _ = make_verification_records(
            consumed_at=now,
        )

    else:
        token_record, _, _ = make_verification_records(
            purpose="delete",
        )

    db, _ = make_db()
    db.scalar.return_value = token_record

    with pytest.raises(service.InvalidVerificationTokenError):
        service.verify_waitlist_email(
            db,
            RAW_TOKEN,
        )

    # The waitlist entry and VIP capacity must not be queried for an
    # already invalid token.
    assert db.scalar.call_count == 1


def test_missing_vip_singleton_fails_closed(monkeypatch):
    configure_settings(monkeypatch)

    token_record, entry, _ = make_verification_records()

    db, _ = make_db()
    db.scalar.side_effect = [
        token_record,
        entry,
        None,
    ]

    with pytest.raises(service.VipCapacityInvariantError):
        service.verify_waitlist_email(
            db,
            RAW_TOKEN,
        )

    assert token_record.consumed_at is None
    assert entry.verified_at is None
    assert entry.vip_number is None

# ---------------------------------------------------------------------
# Verification email resend
# ---------------------------------------------------------------------

def _make_resend_entry(*, verified_at=None):
    return WaitlistEntry(
        id=uuid.uuid4(),
        email="Aryan@example.com",
        normalized_email="aryan@example.com",
        consent_accepted_at=datetime.now(timezone.utc),
        consent_version="test-consent-v1",
        reference_code="SB-TEST-RESEND",
        verified_at=verified_at,
    )


def _make_scalars_result(items):
    result = MagicMock()
    result.all.return_value = items
    return result


def test_resend_unknown_email_returns_generic_response(monkeypatch):
    configure_settings(monkeypatch)

    db, added = make_db()
    db.scalar.return_value = None

    response = service.resend_verification_email(
        db,
        "unknown@example.com",
    )

    assert response == service.WaitlistResendVerificationResponse()
    assert added == []
    db.scalars.assert_not_called()
    db.add_all.assert_not_called()
    db.flush.assert_not_called()
    db.execute.assert_called_once()


def test_resend_verified_email_returns_generic_response(monkeypatch):
    configure_settings(monkeypatch)

    entry = _make_resend_entry(
        verified_at=datetime.now(timezone.utc),
    )

    db, added = make_db()
    db.scalar.side_effect = [
        entry.id,
        entry,
    ]
    db.scalars.return_value = _make_scalars_result([])

    response = service.resend_verification_email(
        db,
        "ARYAN@example.com",
    )

    assert response == service.WaitlistResendVerificationResponse()
    assert entry.verified_at is not None

    # No replacement token or email should be created.
    assert added == []
    db.add_all.assert_not_called()
    db.flush.assert_not_called()

    # Only the existing token collection is queried.
    assert db.scalars.call_count == 1


def test_resend_eligible_email_invalidates_old_tokens_and_queues_new_email(
    monkeypatch,
):
    configure_settings(monkeypatch)

    entry = _make_resend_entry()

    old_token = VerificationToken(
        id=uuid.uuid4(),
        waitlist_id=entry.id,
        token_hash="a" * 64,
        purpose="verify",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        consumed_at=None,
    )

    previously_consumed_token = VerificationToken(
        id=uuid.uuid4(),
        waitlist_id=entry.id,
        token_hash="b" * 64,
        purpose="verify",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=12),
        consumed_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    pending_message = EmailOutbox(
        waitlist_id=entry.id,
        purpose="verify",
        encrypted_token="v1.old_nonce.old_ciphertext",
        encryption_key_version="v1",
        status="PENDING",
        attempt_count=0,
        available_at=datetime.now(timezone.utc),
        locked_at=datetime.now(timezone.utc),
        locked_until=datetime.now(timezone.utc) + timedelta(minutes=1),
        locked_by="old-worker",
    )

    db, added = make_db()

    db.scalar.side_effect = [
        entry.id,
        entry,
    ]

    db.scalars.side_effect = [
        _make_scalars_result([
            old_token,
            previously_consumed_token,
        ]),
        _make_scalars_result([
            pending_message,
        ]),
    ]

    monkeypatch.setattr(
        service,
        "_generate_verification_token",
        lambda: RAW_TOKEN,
    )
    encrypt_mock = MagicMock(return_value=ENCRYPTED_ENVELOPE)
    monkeypatch.setattr(service, "encrypt_token", encrypt_mock)

    response = service.resend_verification_email(
        db,
        "  ARYAN@example.com  ",
    )

    assert response == service.WaitlistResendVerificationResponse()

    # Previous tokens are invalidated.
    assert old_token.consumed_at is not None

    # Already-consumed tokens remain consumed.
    assert previously_consumed_token.consumed_at is not None

    # Old pending email is superseded and its lease fields cleared.
    assert pending_message.status == "FAILED"
    assert pending_message.last_error == (
        "Superseded by a newer verification request."
    )
    assert pending_message.locked_at is None
    assert pending_message.locked_until is None
    assert pending_message.locked_by is None

    # Fresh token and outbox message are created.
    new_token = next(
        item for item in added
        if isinstance(item, VerificationToken)
    )
    new_outbox = next(
        item for item in added
        if isinstance(item, EmailOutbox)
    )

    expected_digest = hashlib.sha256(
        RAW_TOKEN.encode("utf-8")
    ).hexdigest()

    assert new_token.waitlist_id == entry.id
    assert new_token.purpose == "verify"
    assert new_token.token_hash == expected_digest
    assert new_token.consumed_at is None

    assert new_outbox.waitlist_id == entry.id
    assert new_outbox.purpose == "verify"
    assert new_outbox.encrypted_token == ENCRYPTED_ENVELOPE
    assert new_outbox.encryption_key_version == "v1"

    # Encryption must receive the raw token only for encryption.
    encrypt_mock.assert_called_once_with(RAW_TOKEN, "verify")

    # Confirm that the transaction-scoped advisory lock is present.
    lock_statement = str(
        db.execute.call_args.args[0].compile(
            dialect=postgresql.dialect()
        )
    )
    assert "pg_advisory_xact_lock" in lock_statement

    assert db.scalars.call_count == 2
    db.flush.assert_called_once()

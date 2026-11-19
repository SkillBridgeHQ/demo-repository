"""
Transactional service layer for the SkillBridge waitlist.

Responsibilities:
- Register waitlist entries atomically.
- Generate and hash verification tokens.
- Store encrypted tokens in the email outbox.
- Verify single-use tokens.
- Allocate VIP numbers using PostgreSQL row-level locking.

This module does not send emails. Email delivery is handled by
the separate background outbox worker.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import NoReturn

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption import encrypt_token
from app.models.email_outbox import EmailOutbox
from app.models.verification_token import VerificationToken
from app.models.vip_capacity import VipCapacity
from app.models.waitlist_entry import WaitlistEntry
from app.schemas.waitlist import (
    WaitlistRegisterRequest,
    WaitlistRegisterResponse,
    WaitlistResendVerificationResponse,
    WaitlistVerifyResponse,
)


# ---------------------------------------------------------------------
# Service exceptions
# ---------------------------------------------------------------------

class WaitlistServiceError(Exception):
    """Base exception for waitlist service errors."""


class ConsentConfigurationError(WaitlistServiceError):
    """Raised when the authoritative consent version is not configured."""


class ConsentVersionMismatchError(WaitlistServiceError):
    """Raised when the client submits a stale consent version."""


class TransactionBoundaryError(WaitlistServiceError):
    """Raised when the service receives a session with an active transaction."""


class InvalidVerificationTokenError(WaitlistServiceError):
    """Raised for invalid, expired, consumed, or unknown verification tokens."""


class VipCapacityInvariantError(WaitlistServiceError):
    """Raised when the required VIP singleton row is missing or invalid."""


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

DUPLICATE_EMAIL_CONSTRAINT = "uq_waitlist_entries_normalized_email"


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _require_clean_session(db: Session) -> None:
    """
    Require a session without an active transaction.

    Each public service operation owns its transaction and commits or
    rolls it back atomically. The API dependency should provide a fresh
    SQLAlchemy Session without executing queries beforehand.
    """
    if db.in_transaction():
        raise TransactionBoundaryError(
            "Waitlist service requires a session without an active transaction."
        )


def _get_constraint_name(exc: IntegrityError) -> str | None:
    """
    Extract the PostgreSQL constraint name from a DBAPI IntegrityError.

    Psycopg exposes the violated constraint through orig.diag.
    """
    original = getattr(exc, "orig", None)
    diagnostic = getattr(original, "diag", None)

    constraint_name = getattr(diagnostic, "constraint_name", None)

    if constraint_name:
        return str(constraint_name)

    return None


def _generate_reference_code() -> str:
    """
    Generate a cryptographically random public reference code.

    The generated value fits within the model's String(32) column.
    """
    return f"SB-{secrets.token_hex(12).upper()}"


def _generate_verification_token() -> str:
    """
    Generate a cryptographically secure URL-safe verification token.

    32 random bytes produce a token compatible with the verification
    request schema.
    """
    return secrets.token_urlsafe(32)


def _hash_token(token: str) -> str:
    """Return the SHA-256 hexadecimal digest of a raw token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_expired(expires_at: datetime, now: datetime) -> bool:
    """Compare timestamps safely, including naive SQLite test timestamps."""
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)

    return expires_at <= now


def _raise_invalid_token() -> NoReturn:
    """
    Raise a deliberately generic verification error.

    Do not reveal whether a token exists, expired, or was already consumed.
    """
    raise InvalidVerificationTokenError(
        "The verification link is invalid, expired, or has already been used."
    )


# ---------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------

def register_waitlist_entry(
    db: Session,
    payload: WaitlistRegisterRequest,
) -> WaitlistRegisterResponse:
    """
    Register a user and create the verification email outbox record.

    Atomic transaction:
        1. Create WaitlistEntry.
        2. Create VerificationToken containing only the SHA-256 hash.
        3. Create EmailOutbox containing only the encrypted raw token.

    Duplicate email registrations receive the same generic response.
    No email existence information is exposed to the caller.

    The raw verification token is never returned or logged.
    """

    _require_clean_session(db)

    settings = get_settings()

    # The server, not the client, defines the authoritative consent version.
    authoritative_consent_version = settings.CONSENT_VERSION

    if not authoritative_consent_version:
        raise ConsentConfigurationError(
            "The authoritative CONSENT_VERSION is not configured."
        )

    if payload.consent_version != authoritative_consent_version:
        raise ConsentVersionMismatchError(
            "The consent version is no longer current. "
            "Reload the page and accept the current consent notice."
        )

    # Normalize email for case-insensitive uniqueness.
    submitted_email = str(payload.email).strip()
    normalized_email = submitted_email.casefold()

    now = _utc_now()

    # Generate token material before opening the database transaction.
    raw_token = _generate_verification_token()
    token_digest = _hash_token(raw_token)

    encrypted_envelope = encrypt_token(raw_token, "verify")

    # The envelope contains: version.nonce.ciphertext
    envelope_version, separator, _ = encrypted_envelope.partition(".")

    if (
        not separator
        or envelope_version != settings.ENCRYPTION_ACTIVE_KEY_VERSION
    ):
        raise WaitlistServiceError(
            "The encrypted token envelope has an unexpected key version."
        )

    expires_at = now + timedelta(
        minutes=settings.VERIFICATION_TOKEN_TTL_MINUTES
    )

    entry = WaitlistEntry(
        email=submitted_email,
        normalized_email=normalized_email,
        consent_accepted_at=now,
        consent_version=authoritative_consent_version,
        reference_code=_generate_reference_code(),
    )

    try:
        with db.begin():
            # 1. Insert the waitlist entry.
            db.add(entry)
            db.flush()

            # The UUID is available after the flush.
            if entry.id is None:
                raise WaitlistServiceError(
                    "Failed to generate a waitlist entry identifier."
                )

            # 2. Store only the token hash.
            verification_record = VerificationToken(
                waitlist_id=entry.id,
                token_hash=token_digest,
                purpose="verify",
                expires_at=expires_at,
            )

            # 3. Store the encrypted token for background email delivery.
            outbox_record = EmailOutbox(
                waitlist_id=entry.id,
                purpose="verify",
                encrypted_token=encrypted_envelope,
                encryption_key_version=envelope_version,
            )

            db.add_all([
                verification_record,
                outbox_record,
            ])

            db.flush()

    except IntegrityError as exc:
        # Only the normalized-email uniqueness violation is treated
        # as a duplicate registration. Other database failures propagate.
        if _get_constraint_name(exc) == DUPLICATE_EMAIL_CONSTRAINT:
            return WaitlistRegisterResponse()

        raise

    # Identical response for new and duplicate registrations.
    return WaitlistRegisterResponse()


# ---------------------------------------------------------------------
# Email verification and VIP allocation
# ---------------------------------------------------------------------

def verify_waitlist_email(
    db: Session,
    raw_token: str,
) -> WaitlistVerifyResponse:
    """
    Verify a waitlist user's email using a single-use verification token.

    Atomic transaction:
        1. Hash the submitted token.
        2. Lock and validate the verification-token row.
        3. Lock the corresponding waitlist entry.
        4. Lock the VIP singleton row.
        5. Mark the token consumed.
        6. Mark the entry verified.
        7. Allocate a VIP number if capacity remains.

    VIP allocation uses the singleton counter and row-level locking.
    It never uses COUNT(*) to determine availability.
    """

    _require_clean_session(db)

    settings = get_settings()

    now = _utc_now()
    token_digest = _hash_token(raw_token)

    reference_code: str | None = None
    allocated_vip_number: int | None = None

    with db.begin():

        # 1. Lock the token row to prevent simultaneous consumption.
        token_record = db.scalar(
            select(VerificationToken)
            .where(VerificationToken.token_hash == token_digest)
            .with_for_update()
        )

        if token_record is None:
            _raise_invalid_token()

        if token_record.purpose != "verify":
            _raise_invalid_token()

        if token_record.consumed_at is not None:
            _raise_invalid_token()

        if _is_expired(token_record.expires_at, now):
            _raise_invalid_token()

        # 2. Lock the associated waitlist entry.
        entry = db.scalar(
            select(WaitlistEntry)
            .where(WaitlistEntry.id == token_record.waitlist_id)
            .with_for_update()
        )

        if entry is None:
            _raise_invalid_token()

        # A previously verified entry must not receive another VIP slot.
        if entry.verified_at is not None:
            _raise_invalid_token()

        # 3. Lock the singleton VIP capacity row.
        capacity = db.scalar(
            select(VipCapacity)
            .where(VipCapacity.id == 1)
            .with_for_update()
        )

        # Do not silently create a replacement singleton row.
        # The database migration is responsible for seeding it.
        if capacity is None:
            raise VipCapacityInvariantError(
                "VIP capacity singleton row is missing. "
                "Apply the required database seed migration."
            )

        configured_cap = min(settings.EARLY_ACCESS_CAP, 500)

        if not 0 <= capacity.allocated_count <= 500:
            raise VipCapacityInvariantError(
                "VIP capacity counter is outside the permitted range."
            )

        # 4. Consume the token and verify the email.
        token_record.consumed_at = now
        entry.verified_at = now

        # 5. Allocate the next VIP number when capacity remains.
        if capacity.allocated_count < configured_cap:
            capacity.allocated_count += 1
            entry.vip_number = capacity.allocated_count

        # If the cap is full, the user remains verified without a VIP number.
        reference_code = entry.reference_code
        allocated_vip_number = entry.vip_number

        db.flush()

    # The transaction has successfully committed at this point.
    if reference_code is None:
        raise WaitlistServiceError(
            "Verification completed without a reference code."
        )

    if allocated_vip_number is None:
        message = (
            "Email verified successfully. "
            "VIP early-access capacity is currently full."
        )
    else:
        message = "Email verified successfully."

    return WaitlistVerifyResponse(
        verified=True,
        reference_code=reference_code,
        vip_number=allocated_vip_number,
        message=message,
    )

# ---------------------------------------------------------------------
# Verification email resend
# ---------------------------------------------------------------------

def resend_verification_email(
    db: Session,
    email: str,
) -> WaitlistResendVerificationResponse:
    """
    Queue a fresh verification email for an eligible waitlist entry.

    Privacy:
        - Unknown and already-verified addresses receive the same response.
        - Email addresses and raw tokens are never logged or returned.

    Atomic transaction:
        1. Find the entry using its normalized email.
        2. Lock its verification-token rows.
        3. Lock the corresponding waitlist entry.
        4. Invalidate all previous verification tokens.
        5. Supersede older PENDING verification emails.
        6. Store a new token hash and encrypted outbox payload.

    Lock ordering follows verify_waitlist_email():
        VerificationToken -> WaitlistEntry

    PROCESSING outbox rows are deliberately left to the worker's
    lease mechanism. Their old tokens are invalidated, so they cannot
    be used to verify the account.
    """

    _require_clean_session(db)

    normalized_email = str(email).strip().casefold()

    if not normalized_email:
        return WaitlistResendVerificationResponse()

    settings = get_settings()
    now = _utc_now()

    with db.begin():

        # Serialize resend transactions for the same normalized email.
        # The HMAC-based Redis rate limiter remains separate from this
        # database transaction lock.
        email_lock_digest = hashlib.sha256(
            normalized_email.encode("utf-8")
        ).digest()

        email_lock_id = int.from_bytes(
            email_lock_digest[:4],
            byteorder="big",
            signed=True,
        )

        # Dedicated two-key PostgreSQL advisory-lock namespace.
        # Transaction-scoped: PostgreSQL releases it automatically
        # when this transaction commits or rolls back.
        db.execute(
            select(
                func.pg_advisory_xact_lock(
                    1397449298,
                    email_lock_id,
                )
            )
        )

        # 1. Resolve the entry ID without locking the entry row.
        entry_id = db.scalar(
            select(WaitlistEntry.id).where(
                WaitlistEntry.normalized_email == normalized_email
            )
        )

        if entry_id is None:
            return WaitlistResendVerificationResponse()

        # 2. Lock verification tokens before locking the entry.
        # Lock all verification tokens, including previously consumed
        # or expired ones, to serialize with verification operations.
        verification_records = db.scalars(
            select(VerificationToken)
            .where(
                VerificationToken.waitlist_id == entry_id,
                VerificationToken.purpose == "verify",
            )
            .order_by(VerificationToken.id.asc())
            .with_for_update()
        ).all()

        # 3. Lock the waitlist entry.
        entry = db.scalar(
            select(WaitlistEntry)
            .where(WaitlistEntry.id == entry_id)
            .with_for_update()
        )

        if entry is None or entry.verified_at is not None:
            return WaitlistResendVerificationResponse()

        # 4. Invalidate every previous verification token.
        for token_record in verification_records:
            if token_record.consumed_at is None:
                token_record.consumed_at = now

        # 5. Supersede older messages that have not yet been claimed.
        # PROCESSING rows are not modified because they belong to a
        # worker lease. Their tokens are invalidated above.
        pending_messages = db.scalars(
            select(EmailOutbox)
            .where(
                EmailOutbox.waitlist_id == entry.id,
                EmailOutbox.purpose == "verify",
                EmailOutbox.status == "PENDING",
            )
            .order_by(EmailOutbox.id.asc())
            .with_for_update()
        ).all()

        for message in pending_messages:
            message.status = "FAILED"
            message.last_error = (
                "Superseded by a newer verification request."
            )
            message.locked_at = None
            message.locked_until = None
            message.locked_by = None

        # 6. Generate fresh token material.
        raw_token = _generate_verification_token()
        token_digest = _hash_token(raw_token)
        encrypted_envelope = encrypt_token(raw_token, "verify")

        envelope_version, separator, _ = encrypted_envelope.partition(".")

        if (
            not separator
            or envelope_version != settings.ENCRYPTION_ACTIVE_KEY_VERSION
        ):
            raise WaitlistServiceError(
                "The encrypted token envelope has an unexpected key version."
            )

        expires_at = now + timedelta(
            minutes=settings.VERIFICATION_TOKEN_TTL_MINUTES
        )

        new_token_record = VerificationToken(
            waitlist_id=entry.id,
            token_hash=token_digest,
            purpose="verify",
            expires_at=expires_at,
        )

        new_outbox_record = EmailOutbox(
            waitlist_id=entry.id,
            purpose="verify",
            encrypted_token=encrypted_envelope,
            encryption_key_version=envelope_version,
        )

        db.add_all([
            new_token_record,
            new_outbox_record,
        ])

        db.flush()

    # Same response for unknown, verified, and eligible addresses.
    return WaitlistResendVerificationResponse()

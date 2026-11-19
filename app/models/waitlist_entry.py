import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class WaitlistEntry(Base):
    """
    SQLAlchemy 2.x model representing a user registered on the SkillBridge waitlist.
    Handles core identity, consent tracking, verification status, and VIP queue allocation.
    """
    __tablename__ = "waitlist_entries"

    # Identity
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # Email handling
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # Legal / Consent
    consent_accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False
    )
    consent_version: Mapped[str] = mapped_column(String(50), nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # Verification
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    # VIP Allocation
    vip_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reference_code: Mapped[str] = mapped_column(String(32), nullable=False)

    # Constraints and Indexes
    __table_args__ = (
        # Explicit Unique Constraints
        UniqueConstraint(
            "normalized_email",
            name="uq_waitlist_entries_normalized_email"
        ),
        UniqueConstraint(
            "reference_code",
            name="uq_waitlist_entries_reference_code"
        ),
        UniqueConstraint(
            "vip_number",
            name="uq_waitlist_entries_vip_number"
        ),

        # Explicit Check Constraints
        CheckConstraint(
            "vip_number IS NULL OR (vip_number >= 1 AND vip_number <= 500)",
            name="ck_waitlist_entries_vip_bounds"
        ),
        CheckConstraint(
            "vip_number IS NULL OR verified_at IS NOT NULL",
            name="ck_waitlist_entries_vip_requires_verification"
        ),

        # Performance Indexes (avoiding duplication with unique constraints)
        Index("ix_waitlist_entries_created_at", "created_at"),
        Index("ix_waitlist_entries_verified_at", "verified_at"),
    )

    def __repr__(self) -> str:
        """
        Secure string representation for logging and debugging.
        Explicitly masks sensitive PII (emails) and secure tokens (reference_code).
        """
        is_verified = self.verified_at is not None
        return (
            f"<WaitlistEntry(id={self.id}, "
            f"verified={is_verified}, "
            f"vip={self.vip_number})>"
        )
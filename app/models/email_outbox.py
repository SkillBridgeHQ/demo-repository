import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class EmailOutbox(Base):
    """
    SQLAlchemy 2.x model representing the email delivery queue for transactional emails.
    Supports a robust background worker pattern utilizing row-level locks and leased execution.
    Raw tokens are strictly prohibited here; only AES-encrypted payloads are stored.
    """
    __tablename__ = "email_outbox"

    # Identity
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # Waitlist Reference
    waitlist_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "waitlist_entries.id",
            ondelete="CASCADE",
            name="fk_email_outbox_waitlist_id_waitlist_entries"
        ),
        nullable=False
    )

    # Email Purpose
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)

    # Encrypted Token (Payload)
    encrypted_token: Mapped[str] = mapped_column(Text, nullable=False)
    encryption_key_version: Mapped[str] = mapped_column(String(10), nullable=False)

    # Delivery Status
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default=text("'PENDING'")
    )

    # Retry Management
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )

    # Worker Lease Management
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Delivery Result
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now()
    )

    # Constraints and Indexes
    __table_args__ = (
        # Check Constraints
        CheckConstraint(
            "purpose IN ('verify', 'delete')",
            name="ck_email_outbox_purpose"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')",
            name="ck_email_outbox_status"
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_email_outbox_attempt_count"
        ),

        # Performance Indexes
        Index("ix_email_outbox_waitlist_id", "waitlist_id"),
        Index("ix_email_outbox_status_available_at", "status", "available_at"),
        Index("ix_email_outbox_status_locked_until", "status", "locked_until"),
        Index("ix_email_outbox_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        """
        Secure string representation for logging and debugging.
        Explicitly masks `encrypted_token`, `last_error`, and any identifying lease data.
        """
        return (
            f"<EmailOutbox(id={self.id}, "
            f"purpose='{self.purpose}', "
            f"status='{self.status}', "
            f"attempt_count={self.attempt_count})>"
        )
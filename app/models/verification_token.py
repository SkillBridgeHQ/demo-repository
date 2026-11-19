import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class VerificationToken(Base):
    """
    SQLAlchemy 2.x model representing a secure, single-use token for waitlist verification or deletion.
    To prevent token exposure in the event of a database leak, this table stores only the
    SHA-256 hexadecimal digest of the raw token.
    """
    __tablename__ = "verification_tokens"

    # Identity
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # Waitlist Relationship
    waitlist_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey(
            "waitlist_entries.id",
            ondelete="CASCADE",
            name="fk_verification_tokens_waitlist_id_waitlist_entries"
        ),
        nullable=False
    )

    # Token Security
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Token Purpose
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)

    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    # Constraints and Indexes
    __table_args__ = (
        # Explicit Unique Constraints
        UniqueConstraint(
            "token_hash",
            name="uq_verification_tokens_token_hash"
        ),

        # Explicit Check Constraints
        CheckConstraint(
            "purpose IN ('verify', 'delete')",
            name="ck_verification_tokens_purpose"
        ),
        CheckConstraint(
            "char_length(token_hash) = 64",
            name="ck_verification_tokens_hash_length"
        ),

        # Performance Indexes
        # (A standalone index for waitlist_id is omitted because the leftmost column
        # of the composite index below already satisfies single-column queries)
        Index("ix_verification_tokens_expires_at", "expires_at"),
        Index("ix_verification_tokens_waitlist_purpose", "waitlist_id", "purpose"),
    )

    def __repr__(self) -> str:
        """
        Secure string representation for logging and debugging.
        Explicitly masks the sensitive token_hash.
        """
        is_consumed = self.consumed_at is not None
        return (
            f"<VerificationToken(id={self.id}, "
            f"purpose='{self.purpose}', "
            f"consumed={is_consumed})>"
        )
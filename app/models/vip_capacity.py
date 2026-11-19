from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class VipCapacity(Base):
    """
    SQLAlchemy 2.x model representing a singleton tracking table for the
    SkillBridge waitlist early access VIP allocation capacity (capped at 500).
    """
    __tablename__ = "vip_capacity"

    # Singleton Identity (Strictly constrained to ID = 1)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)

    # Allocation Counter (0 to 500 inclusive)
    allocated_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0"
    )

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

    # Database Constraints
    __table_args__ = (
        CheckConstraint(
            "id = 1",
            name="ck_vip_capacity_singleton_id"
        ),
        CheckConstraint(
            "allocated_count >= 0 AND allocated_count <= 500",
            name="ck_vip_capacity_allocated_count_bounds"
        ),
    )

    def __repr__(self) -> str:
        """
        Clean, safe string representation showing only singleton metadata and current count.
        """
        return f"<VipCapacity(id={self.id}, allocated_count={self.allocated_count})>"
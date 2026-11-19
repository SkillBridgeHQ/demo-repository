"""seed_vip_capacity_singleton

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-11-14 19:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a1'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Define a lightweight SQLAlchemy table representation to securely and
# deterministically issue inserts and deletes without importing application ORM models.
vip_capacity_table = sa.table(
    'vip_capacity',
    sa.column('id', sa.Integer),
    sa.column('allocated_count', sa.Integer)
)


def upgrade() -> None:
    # Insert exactly one singleton row into vip_capacity.
    # We deliberately omit the timestamp columns so that PostgreSQL's native
    # server_default (now()) accurately populates them during execution.
    # Note: Deliberately avoiding ON CONFLICT DO NOTHING so that the migration
    # visibly fails if the database is in an unexpected state.
    op.bulk_insert(
        vip_capacity_table,
        [
            {'id': 1, 'allocated_count': 0}
        ]
    )


def downgrade() -> None:
    # Safely target and delete ONLY the specific singleton row inserted by upgrade().
    # This prevents dropping the table or modifying the schema definition.
    op.execute(
        vip_capacity_table.delete().where(vip_capacity_table.c.id == 1)
    )
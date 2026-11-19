"""initial_waitlist_schema

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-11-14 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. CREATE waitlist_entries
    op.create_table(
        'waitlist_entries',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('normalized_email', sa.String(length=320), nullable=False),
        sa.Column('consent_accepted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consent_version', sa.String(length=50), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('vip_number', sa.Integer(), nullable=True),
        sa.Column('reference_code', sa.String(length=32), nullable=False),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('normalized_email', name='uq_waitlist_entries_normalized_email'),
        sa.UniqueConstraint('reference_code', name='uq_waitlist_entries_reference_code'),
        sa.UniqueConstraint('vip_number', name='uq_waitlist_entries_vip_number'),

        sa.CheckConstraint('vip_number IS NULL OR (vip_number >= 1 AND vip_number <= 500)', name='ck_waitlist_entries_vip_bounds'),
        sa.CheckConstraint('vip_number IS NULL OR verified_at IS NOT NULL', name='ck_waitlist_entries_vip_requires_verification')
    )

    op.create_index('ix_waitlist_entries_created_at', 'waitlist_entries', ['created_at'], unique=False)
    op.create_index('ix_waitlist_entries_verified_at', 'waitlist_entries', ['verified_at'], unique=False)

    # 2. CREATE vip_capacity (Standalone Table)
    op.create_table(
        'vip_capacity',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('allocated_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),

        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint('id = 1', name='ck_vip_capacity_singleton_id'),
        sa.CheckConstraint('allocated_count >= 0 AND allocated_count <= 500', name='ck_vip_capacity_allocated_count_bounds')
    )

    # 3. CREATE verification_tokens
    op.create_table(
        'verification_tokens',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('waitlist_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),

        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['waitlist_id'], ['waitlist_entries.id'], name='fk_verification_tokens_waitlist_id_waitlist_entries', ondelete='CASCADE'),

        sa.UniqueConstraint('token_hash', name='uq_verification_tokens_token_hash'),
        sa.CheckConstraint("purpose IN ('verify', 'delete')", name='ck_verification_tokens_purpose'),
        sa.CheckConstraint('char_length(token_hash) = 64', name='ck_verification_tokens_hash_length')
    )

    op.create_index('ix_verification_tokens_expires_at', 'verification_tokens', ['expires_at'], unique=False)
    op.create_index('ix_verification_tokens_waitlist_purpose', 'verification_tokens', ['waitlist_id', 'purpose'], unique=False)

    # 4. CREATE email_outbox
    op.create_table(
        'email_outbox',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('waitlist_id', sa.Uuid(), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('encrypted_token', sa.Text(), nullable=False),
        sa.Column('encryption_key_version', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column('attempt_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_by', sa.String(length=128), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),

        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['waitlist_id'], ['waitlist_entries.id'], name='fk_email_outbox_waitlist_id_waitlist_entries', ondelete='CASCADE'),

        sa.CheckConstraint("purpose IN ('verify', 'delete')", name='ck_email_outbox_purpose'),
        sa.CheckConstraint("status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')", name='ck_email_outbox_status'),
        sa.CheckConstraint('attempt_count >= 0', name='ck_email_outbox_attempt_count')
    )

    op.create_index('ix_email_outbox_waitlist_id', 'email_outbox', ['waitlist_id'], unique=False)
    op.create_index('ix_email_outbox_status_available_at', 'email_outbox', ['status', 'available_at'], unique=False)
    op.create_index('ix_email_outbox_status_locked_until', 'email_outbox', ['status', 'locked_until'], unique=False)
    op.create_index('ix_email_outbox_created_at', 'email_outbox', ['created_at'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse dependency order to prevent foreign key violations
    op.drop_table('email_outbox')
    op.drop_table('verification_tokens')
    op.drop_table('vip_capacity')
    op.drop_table('waitlist_entries')
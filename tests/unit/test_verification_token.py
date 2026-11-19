import base64
import importlib
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint, Uuid


@pytest.fixture
def model_module(monkeypatch, tmp_path):
    """
    Safely isolates the environment, configures test variables,
    and conditionally imports/reloads the session, waitlist, and token modules exactly once.
    Ensures engine cleanup executes even if a model import fails.
    """
    monkeypatch.chdir(tmp_path)

    valid_key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EMAIL_MODE", "console")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db")
    monkeypatch.setenv("ENCRYPTION_KEY_V1", valid_key)

    from app.core.config import get_settings
    get_settings.cache_clear()

    # 1. Load Session
    if "app.db.session" in sys.modules:
        try:
            sys.modules["app.db.session"].engine.dispose()
        except AttributeError:
            pass
        session_mod = importlib.reload(sys.modules["app.db.session"])
    else:
        session_mod = importlib.import_module("app.db.session")

    # Capture the newly created engine immediately
    real_engine = session_mod.engine

    try:
        # 2. Load WaitlistEntry (First model)
        if "app.models.waitlist_entry" in sys.modules:
            entry_mod = importlib.reload(sys.modules["app.models.waitlist_entry"])
        else:
            entry_mod = importlib.import_module("app.models.waitlist_entry")

        # 3. Load VerificationToken (Second model)
        if "app.models.verification_token" in sys.modules:
            token_mod = importlib.reload(sys.modules["app.models.verification_token"])
        else:
            token_mod = importlib.import_module("app.models.verification_token")

        yield token_mod, entry_mod, session_mod
    finally:
        real_engine.dispose()
        get_settings.cache_clear()


# ==========================================
# 2. Table and Metadata Tests
# ==========================================

def test_verification_token_metadata(model_module):
    token_mod, entry_mod, session_mod = model_module
    model = token_mod.VerificationToken

    assert issubclass(model, session_mod.Base)
    assert model.__tablename__ == "verification_tokens"

    # Explicitly confirm both models belong to the exact same metadata object instance
    assert entry_mod.WaitlistEntry.metadata is session_mod.Base.metadata
    assert token_mod.VerificationToken.metadata is session_mod.Base.metadata

    # Verify Alembic compatibility - both tables share the same registry
    assert "verification_tokens" in session_mod.Base.metadata.tables
    assert "waitlist_entries" in session_mod.Base.metadata.tables

    table = model.__table__
    expected_columns = {
        "id", "waitlist_id", "token_hash", "purpose",
        "created_at", "expires_at", "consumed_at"
    }
    actual_columns = set(table.columns.keys())
    assert expected_columns == actual_columns


# ==========================================
# 3. Column Type and Nullability Tests
# ==========================================

def test_verification_token_columns(model_module):
    token_mod, _, _ = model_module
    table = token_mod.VerificationToken.__table__

    # ID
    id_col = table.columns.id
    assert isinstance(id_col.type, Uuid)
    assert id_col.type.as_uuid is True
    assert id_col.primary_key is True

    assert id_col.default is not None
    default_fn = id_col.default.arg
    assert callable(default_fn)
    generated_uuid = default_fn(None)  # Provide None as the execution context
    assert isinstance(generated_uuid, uuid.UUID)
    assert generated_uuid.version == 4

    # Waitlist ID
    waitlist_id_col = table.columns.waitlist_id
    assert isinstance(waitlist_id_col.type, Uuid)
    assert waitlist_id_col.type.as_uuid is True
    assert waitlist_id_col.nullable is False

    # Token Hash
    token_hash_col = table.columns.token_hash
    assert isinstance(token_hash_col.type, String)
    assert token_hash_col.type.length == 64
    assert token_hash_col.nullable is False
    assert token_hash_col.default is None

    # Purpose
    purpose_col = table.columns.purpose
    assert isinstance(purpose_col.type, String)
    assert purpose_col.type.length == 20
    assert purpose_col.nullable is False
    assert purpose_col.default is None

    # Timestamps
    created_at_col = table.columns.created_at
    assert isinstance(created_at_col.type, DateTime)
    assert created_at_col.type.timezone is True
    assert created_at_col.nullable is False
    assert created_at_col.server_default is not None

    expires_at_col = table.columns.expires_at
    assert isinstance(expires_at_col.type, DateTime)
    assert expires_at_col.type.timezone is True
    assert expires_at_col.nullable is False
    assert expires_at_col.default is None

    consumed_at_col = table.columns.consumed_at
    assert isinstance(consumed_at_col.type, DateTime)
    assert consumed_at_col.type.timezone is True
    assert consumed_at_col.nullable is True
    assert consumed_at_col.default is None


# ==========================================
# 4. Foreign Key Tests
# ==========================================

def test_verification_token_foreign_keys(model_module):
    token_mod, _, _ = model_module
    table = token_mod.VerificationToken.__table__

    foreign_keys = list(table.foreign_keys)
    assert len(foreign_keys) == 1

    fk = foreign_keys[0]
    assert fk.column.table.name == "waitlist_entries"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"
    assert fk.name == "fk_verification_tokens_waitlist_id_waitlist_entries"

    # Ensure compatible types (both are UUID)
    assert isinstance(fk.column.type, Uuid)
    assert isinstance(fk.parent.type, Uuid)


# ==========================================
# 5. Unique Constraint Tests
# ==========================================

def test_verification_token_unique_constraints(model_module):
    token_mod, _, _ = model_module
    table = token_mod.VerificationToken.__table__

    unique_constraints = [c for c in table.constraints if isinstance(c, UniqueConstraint)]

    assert len(unique_constraints) == 1
    constraint = unique_constraints[0]

    assert constraint.name == "uq_verification_tokens_token_hash"
    constrained_columns = {col.name for col in constraint.columns}
    assert constrained_columns == {"token_hash"}


# ==========================================
# 6. Check Constraint Tests
# ==========================================

def test_verification_token_check_constraints(model_module):
    token_mod, _, _ = model_module
    table = token_mod.VerificationToken.__table__

    check_constraints = [c for c in table.constraints if isinstance(c, CheckConstraint)]

    expected_checks = {
        "ck_verification_tokens_purpose": "purpose IN ('verify', 'delete')",
        "ck_verification_tokens_hash_length": "char_length(token_hash) = 64"
    }

    assert len(check_constraints) == len(expected_checks)

    for constraint in check_constraints:
        assert constraint.name in expected_checks
        assert str(constraint.sqltext) == expected_checks[constraint.name]


# ==========================================
# 7. Index Tests
# ==========================================

def test_verification_token_indexes(model_module):
    token_mod, _, _ = model_module
    table = token_mod.VerificationToken.__table__

    expected_indexes = {
        "ix_verification_tokens_expires_at": ["expires_at"],
        "ix_verification_tokens_waitlist_purpose": ["waitlist_id", "purpose"]
    }

    # Table should only have explicit indexes mapped
    assert len(table.indexes) == len(expected_indexes)

    for index in table.indexes:
        assert index.name in expected_indexes

        # Verify columns and their correct order (critical for composite indexes)
        indexed_columns = [col.name for col in index.columns]
        assert indexed_columns == expected_indexes[index.name]

    # Ensure redundant standalone indexes do NOT exist
    index_names = {idx.name for idx in table.indexes}
    assert "ix_verification_tokens_waitlist_id" not in index_names
    assert "ix_verification_tokens_token_hash" not in index_names


# ==========================================
# 8. Privacy Test
# ==========================================

def test_verification_token_privacy_repr(model_module):
    token_mod, _, _ = model_module

    # Explicitly verify the model does not have a raw token column exposed or stored
    assert not hasattr(token_mod.VerificationToken, "raw_token")
    assert not hasattr(token_mod.VerificationToken, "token")

    dummy_hash = "a" * 64

    # Create transient in-memory instance
    token = token_mod.VerificationToken(
        id=uuid.uuid4(),
        waitlist_id=uuid.uuid4(),
        token_hash=dummy_hash,
        purpose="verify",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc)
    )

    repr_string = repr(token)

    # Assert sensitive tokens are omitted
    assert dummy_hash not in repr_string

    # Assert acceptable state tracking is present
    assert str(token.id) in repr_string
    assert "purpose='verify'" in repr_string
    assert "consumed=False" in repr_string


# ==========================================
# 9. Alembic Metadata Compatibility
# ==========================================

def test_alembic_metadata_compatibility(model_module):
    _, _, session_mod = model_module

    metadata = session_mod.Base.metadata

    # Confirm both models map to the unified registry cleanly
    assert "verification_tokens" in metadata.tables
    assert "waitlist_entries" in metadata.tables

    tokens_table = metadata.tables["verification_tokens"]
    waitlist_table = metadata.tables["waitlist_entries"]

    # Resolve the foreign key to prove the relationship exists structurally in metadata
    fk = list(tokens_table.foreign_keys)[0]
    assert fk.column is waitlist_table.c.id
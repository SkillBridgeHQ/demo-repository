import base64
import importlib
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, Uuid
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable


@pytest.fixture
def model_module(monkeypatch, tmp_path):
    """
    Isolates the environment and conditionally imports or reloads the session,
    waitlist, token, VIP, and email outbox modules exactly once.
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
        # 2. Load WaitlistEntry
        if "app.models.waitlist_entry" in sys.modules:
            entry_mod = importlib.reload(sys.modules["app.models.waitlist_entry"])
        else:
            entry_mod = importlib.import_module("app.models.waitlist_entry")

        # 3. Load VerificationToken
        if "app.models.verification_token" in sys.modules:
            token_mod = importlib.reload(sys.modules["app.models.verification_token"])
        else:
            token_mod = importlib.import_module("app.models.verification_token")

        # 4. Load VipCapacity
        if "app.models.vip_capacity" in sys.modules:
            vip_mod = importlib.reload(sys.modules["app.models.vip_capacity"])
        else:
            vip_mod = importlib.import_module("app.models.vip_capacity")

        # 5. Load EmailOutbox
        if "app.models.email_outbox" in sys.modules:
            outbox_mod = importlib.reload(sys.modules["app.models.email_outbox"])
        else:
            outbox_mod = importlib.import_module("app.models.email_outbox")

        yield outbox_mod, session_mod
    finally:
        real_engine.dispose()
        get_settings.cache_clear()


# ==========================================
# 2. Table and Alembic Metadata Tests
# ==========================================

def test_email_outbox_metadata(model_module):
    outbox_mod, session_mod = model_module
    model = outbox_mod.EmailOutbox

    assert issubclass(model, session_mod.Base)
    assert model.__tablename__ == "email_outbox"

    # Explicitly confirm the model belongs to the exact same metadata object instance
    assert model.metadata is session_mod.Base.metadata

    # Verify Alembic compatibility - all required tables share the unified registry
    tables = session_mod.Base.metadata.tables
    assert "email_outbox" in tables
    assert "waitlist_entries" in tables
    assert "verification_tokens" in tables
    assert "vip_capacity" in tables

    table = model.__table__
    expected_columns = {
        "id", "waitlist_id", "purpose", "encrypted_token", "encryption_key_version",
        "status", "attempt_count", "available_at", "locked_at", "locked_until",
        "locked_by", "sent_at", "last_error", "created_at", "updated_at"
    }
    actual_columns = set(table.columns.keys())
    assert expected_columns == actual_columns


# ==========================================
# 3. Column Type and Nullability Tests
# ==========================================

def test_email_outbox_columns(model_module):
    outbox_mod, _ = model_module
    table = outbox_mod.EmailOutbox.__table__

    # Identity
    id_col = table.columns.id
    assert isinstance(id_col.type, Uuid)
    assert id_col.type.as_uuid is True
    assert id_col.primary_key is True
    assert id_col.nullable is False

    assert id_col.default is not None
    default_fn = id_col.default.arg
    assert callable(default_fn)
    generated_uuid = default_fn(None)  # Use SQLAlchemy execution-context
    assert isinstance(generated_uuid, uuid.UUID)
    assert generated_uuid.version == 4

    # Waitlist ID
    waitlist_id_col = table.columns.waitlist_id
    assert isinstance(waitlist_id_col.type, Uuid)
    assert waitlist_id_col.type.as_uuid is True
    assert waitlist_id_col.nullable is False
    assert waitlist_id_col.default is None

    # Purpose
    purpose_col = table.columns.purpose
    assert isinstance(purpose_col.type, String)
    assert purpose_col.type.length == 20
    assert purpose_col.nullable is False
    assert purpose_col.default is None

    # Security / Encryption
    encrypted_col = table.columns.encrypted_token
    assert isinstance(encrypted_col.type, Text)
    assert encrypted_col.nullable is False
    assert encrypted_col.default is None

    enc_version_col = table.columns.encryption_key_version
    assert isinstance(enc_version_col.type, String)
    assert enc_version_col.type.length == 10
    assert enc_version_col.nullable is False
    assert enc_version_col.default is None

    # Lifecycle / Worker
    status_col = table.columns.status
    assert isinstance(status_col.type, String)
    assert status_col.type.length == 20
    assert status_col.nullable is False
    assert status_col.server_default is not None

    attempt_col = table.columns.attempt_count
    assert isinstance(attempt_col.type, Integer)
    assert attempt_col.nullable is False
    assert attempt_col.default is None
    assert attempt_col.server_default is not None

    # Timestamps & Leases (All DateTimes must be timezone-aware)
    for col_name in ["available_at", "locked_at", "locked_until", "sent_at", "created_at", "updated_at"]:
        col = table.columns[col_name]
        assert isinstance(col.type, DateTime)
        assert col.type.timezone is True

    # Timestamps specific rules
    assert table.columns.available_at.nullable is False
    assert table.columns.locked_at.nullable is True
    assert table.columns.locked_until.nullable is True
    assert table.columns.sent_at.nullable is True

    assert table.columns.created_at.nullable is False
    assert table.columns.created_at.server_default is not None

    assert table.columns.updated_at.nullable is False
    assert table.columns.updated_at.server_default is not None
    assert table.columns.updated_at.onupdate is not None

    # Others
    assert isinstance(table.columns.locked_by.type, String)
    assert table.columns.locked_by.type.length == 128
    assert table.columns.locked_by.nullable is True

    assert isinstance(table.columns.last_error.type, Text)
    assert table.columns.last_error.nullable is True


# ==========================================
# 4. PostgreSQL Server Default Validation
# ==========================================

def test_email_outbox_postgres_ddl(model_module):
    outbox_mod, _ = model_module
    table = outbox_mod.EmailOutbox.__table__

    # Compile the table definition into PostgreSQL Dialect DDL
    ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))

    # Assert string literals compile perfectly into PostgreSQL native strings
    assert "DEFAULT 'PENDING'" in ddl

    # Assert integer literals compile safely
    assert "DEFAULT 0" in ddl


# ==========================================
# 5. Foreign Key Tests
# ==========================================

def test_email_outbox_foreign_keys(model_module):
    outbox_mod, session_mod = model_module
    table = outbox_mod.EmailOutbox.__table__
    waitlist_table = session_mod.Base.metadata.tables["waitlist_entries"]

    foreign_keys = list(table.foreign_keys)
    assert len(foreign_keys) == 1

    fk = foreign_keys[0]
    assert fk.name == "fk_email_outbox_waitlist_id_waitlist_entries"
    assert fk.column.table.name == "waitlist_entries"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"

    # Direct metadata comparison
    assert fk.column is waitlist_table.c.id

    # Ensure compatible types (both are UUID)
    assert isinstance(fk.column.type, Uuid)
    assert isinstance(fk.parent.type, Uuid)


# ==========================================
# 6. Check Constraint Tests
# ==========================================

def test_email_outbox_check_constraints(model_module):
    outbox_mod, _ = model_module
    table = outbox_mod.EmailOutbox.__table__

    check_constraints = [c for c in table.constraints if isinstance(c, CheckConstraint)]

    expected_checks = {
        "ck_email_outbox_purpose": "purpose IN ('verify', 'delete')",
        "ck_email_outbox_status": "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')",
        "ck_email_outbox_attempt_count": "attempt_count >= 0"
    }

    assert len(check_constraints) == len(expected_checks)

    for constraint in check_constraints:
        assert constraint.name in expected_checks
        assert str(constraint.sqltext) == expected_checks[constraint.name]


# ==========================================
# 7. Index Tests
# ==========================================

def test_email_outbox_indexes(model_module):
    outbox_mod, _ = model_module
    table = outbox_mod.EmailOutbox.__table__

    expected_indexes = {
        "ix_email_outbox_waitlist_id": ["waitlist_id"],
        "ix_email_outbox_status_available_at": ["status", "available_at"],
        "ix_email_outbox_status_locked_until": ["status", "locked_until"],
        "ix_email_outbox_created_at": ["created_at"]
    }

    assert len(table.indexes) == len(expected_indexes)

    for index in table.indexes:
        assert index.name in expected_indexes

        # Verify columns and their correct order (critical for composite indexes)
        indexed_columns = [col.name for col in index.columns]
        assert indexed_columns == expected_indexes[index.name]


# ==========================================
# 8. Privacy and Security Tests
# ==========================================

def test_email_outbox_privacy_repr(model_module):
    outbox_mod, _ = model_module

    # Explicitly verify the model strictly forbids storing plaintext variants
    # of sensitive identity or token information.
    model = outbox_mod.EmailOutbox
    assert not hasattr(model, "raw_token")
    assert not hasattr(model, "token")
    assert not hasattr(model, "email")
    assert not hasattr(model, "recipient_email")

    dummy_encrypted = "SECRET_PAYLOAD"
    dummy_worker_id = "SECRET_WORKER"
    dummy_error = "SECRET_ERROR"

    # Create transient in-memory instance
    outbox = model(
        id=uuid.uuid4(),
        waitlist_id=uuid.uuid4(),
        purpose="verify",
        encrypted_token=dummy_encrypted,
        encryption_key_version="v1",
        status="PENDING",
        attempt_count=0,
        available_at=datetime.now(timezone.utc),
        locked_by=dummy_worker_id,
        last_error=dummy_error
    )

    repr_string = repr(outbox)

    # Assert sensitive internals are completely omitted from representations
    assert dummy_encrypted not in repr_string
    assert dummy_worker_id not in repr_string
    assert dummy_error not in repr_string

    # Assert acceptable state tracking is present
    assert str(outbox.id) in repr_string
    assert "purpose='verify'" in repr_string
    assert "status='PENDING'" in repr_string
    assert "attempt_count=0" in repr_string


# ==========================================
# 9. Lazy Initialization Constraint
# ==========================================

def test_email_outbox_lazy_connection(model_module):
    """
    Verifies that inspecting metadata and loading models does not execute
    any rogue network connections to the actual PostgreSQL server.
    """
    _, session_mod = model_module

    engine = session_mod.engine
    assert engine.pool.checkedout() == 0
    assert engine.pool.checkedin() == 0
import base64
import importlib
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)


@pytest.fixture
def model_module(monkeypatch, tmp_path):
    """
    Safely isolates the environment, configures test variables,
    and conditionally imports or reloads the session and model modules.
    """
    monkeypatch.chdir(tmp_path)

    valid_key = base64.b64encode(os.urandom(32)).decode("ascii")

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EMAIL_MODE", "console")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db",
    )
    monkeypatch.setenv("ENCRYPTION_KEY_V1", valid_key)

    from app.core.config import get_settings

    get_settings.cache_clear()

    # Dispose of the previous engine before reloading the session module.
    if "app.db.session" in sys.modules:
        try:
            sys.modules["app.db.session"].engine.dispose()
        except AttributeError:
            pass

        session_mod = importlib.reload(sys.modules["app.db.session"])
    else:
        session_mod = importlib.import_module("app.db.session")

    # Register the model against the freshly loaded Base.metadata.
    if "app.models.waitlist_entry" in sys.modules:
        entry_mod = importlib.reload(
            sys.modules["app.models.waitlist_entry"]
        )
    else:
        entry_mod = importlib.import_module(
            "app.models.waitlist_entry"
        )

    real_engine = session_mod.engine

    try:
        yield entry_mod, session_mod
    finally:
        real_engine.dispose()
        get_settings.cache_clear()


# ==========================================
# 1. Table and Declarative Metadata Tests
# ==========================================

def test_waitlist_entry_metadata(model_module):
    entry_mod, session_mod = model_module
    model = entry_mod.WaitlistEntry

    assert issubclass(model, session_mod.Base)
    assert model.__tablename__ == "waitlist_entries"

    # Verify Alembic metadata registration.
    assert "waitlist_entries" in session_mod.Base.metadata.tables

    table = model.__table__

    expected_columns = {
        "id",
        "email",
        "normalized_email",
        "consent_accepted_at",
        "consent_version",
        "created_at",
        "updated_at",
        "verified_at",
        "vip_number",
        "reference_code",
    }

    actual_columns = set(table.columns.keys())
    assert expected_columns == actual_columns


# ==========================================
# 2. Column Type and Nullability Tests
# ==========================================

def test_waitlist_entry_columns(model_module):
    entry_mod, _ = model_module
    table = entry_mod.WaitlistEntry.__table__

    # ID
    id_col = table.columns.id

    assert isinstance(id_col.type, Uuid)
    assert id_col.type.as_uuid is True
    assert id_col.primary_key is True

    default_fn = id_col.default.arg
    assert callable(default_fn)

    # SQLAlchemy callable column defaults receive an execution context.
    generated_uuid = default_fn(None)

    assert isinstance(generated_uuid, uuid.UUID)
    assert generated_uuid.version == 4

    # Emails
    email_col = table.columns.email

    assert isinstance(email_col.type, String)
    assert email_col.type.length == 320
    assert email_col.nullable is False

    norm_email_col = table.columns.normalized_email

    assert isinstance(norm_email_col.type, String)
    assert norm_email_col.type.length == 320
    assert norm_email_col.nullable is False

    # Consent
    consent_at_col = table.columns.consent_accepted_at

    assert isinstance(consent_at_col.type, DateTime)
    assert consent_at_col.type.timezone is True
    assert consent_at_col.nullable is False
    assert consent_at_col.default is None

    consent_ver_col = table.columns.consent_version

    assert isinstance(consent_ver_col.type, String)
    assert consent_ver_col.type.length == 50
    assert consent_ver_col.nullable is False
    assert consent_ver_col.default is None

    # Timestamps
    created_at_col = table.columns.created_at

    assert isinstance(created_at_col.type, DateTime)
    assert created_at_col.type.timezone is True
    assert created_at_col.nullable is False
    assert created_at_col.server_default is not None

    updated_at_col = table.columns.updated_at

    assert isinstance(updated_at_col.type, DateTime)
    assert updated_at_col.type.timezone is True
    assert updated_at_col.nullable is False
    assert updated_at_col.server_default is not None
    assert updated_at_col.onupdate is not None

    # Verification
    verified_at_col = table.columns.verified_at

    assert isinstance(verified_at_col.type, DateTime)
    assert verified_at_col.type.timezone is True
    assert verified_at_col.nullable is True
    assert verified_at_col.default is None

    # VIP allocation
    vip_col = table.columns.vip_number

    assert isinstance(vip_col.type, Integer)
    assert vip_col.nullable is True
    assert vip_col.default is None

    # Reference code
    ref_col = table.columns.reference_code

    assert isinstance(ref_col.type, String)
    assert ref_col.type.length == 32
    assert ref_col.nullable is False


# ==========================================
# 3. Unique Constraints Tests
# ==========================================

def test_waitlist_entry_unique_constraints(model_module):
    entry_mod, _ = model_module
    table = entry_mod.WaitlistEntry.__table__

    unique_constraints = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    expected_uqs = {
        "uq_waitlist_entries_normalized_email": {"normalized_email"},
        "uq_waitlist_entries_reference_code": {"reference_code"},
        "uq_waitlist_entries_vip_number": {"vip_number"},
    }

    assert len(unique_constraints) == len(expected_uqs)

    for constraint in unique_constraints:
        assert constraint.name in expected_uqs

        constrained_columns = {
            column.name for column in constraint.columns
        }

        assert constrained_columns == expected_uqs[constraint.name]


# ==========================================
# 4. Check Constraints Tests
# ==========================================

def test_waitlist_entry_check_constraints(model_module):
    entry_mod, _ = model_module
    table = entry_mod.WaitlistEntry.__table__

    check_constraints = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]

    expected_checks = {
        "ck_waitlist_entries_vip_bounds":
            "vip_number IS NULL OR (vip_number >= 1 AND vip_number <= 500)",
        "ck_waitlist_entries_vip_requires_verification":
            "vip_number IS NULL OR verified_at IS NOT NULL",
    }

    assert len(check_constraints) == len(expected_checks)

    for constraint in check_constraints:
        assert constraint.name in expected_checks
        assert str(constraint.sqltext) == expected_checks[constraint.name]


# ==========================================
# 5. Index Tests
# ==========================================

def test_waitlist_entry_indexes(model_module):
    entry_mod, _ = model_module
    table = entry_mod.WaitlistEntry.__table__

    expected_indexes = {
        "ix_waitlist_entries_created_at": {"created_at"},
        "ix_waitlist_entries_verified_at": {"verified_at"},
    }

    assert len(table.indexes) == len(expected_indexes)

    for index in table.indexes:
        assert index.name in expected_indexes

        indexed_columns = {
            column.name for column in index.columns
        }

        assert indexed_columns == expected_indexes[index.name]


# ==========================================
# 6. Privacy Tests
# ==========================================

def test_waitlist_entry_privacy_repr(model_module):
    entry_mod, _ = model_module

    dummy_email = "SECRET_EMAIL@example.com"
    dummy_normalized = "secret_email@example.com"
    dummy_reference = "SUPER_SECRET_REF_CODE"

    # Create a transient in-memory instance.
    entry = entry_mod.WaitlistEntry(
        id=uuid.uuid4(),
        email=dummy_email,
        normalized_email=dummy_normalized,
        consent_accepted_at=datetime.now(timezone.utc),
        consent_version="v1.0",
        vip_number=42,
        reference_code=dummy_reference,
    )

    repr_string = repr(entry)

    # Sensitive values must not appear in the representation.
    assert dummy_email not in repr_string
    assert dummy_normalized not in repr_string
    assert dummy_reference not in repr_string

    # Safe identifying information should remain available.
    assert str(entry.id) in repr_string
    assert "vip=42" in repr_string
    assert "verified=False" in repr_string
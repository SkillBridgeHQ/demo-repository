import base64
import importlib
import os
import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, DateTime, Integer


@pytest.fixture
def model_module(monkeypatch, tmp_path):
    """
    Safely isolates the environment, configures test variables,
    and conditionally imports/reloads the session and model modules exactly once.
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
        # 2. Load VipCapacity Model
        if "app.models.vip_capacity" in sys.modules:
            vip_mod = importlib.reload(sys.modules["app.models.vip_capacity"])
        else:
            vip_mod = importlib.import_module("app.models.vip_capacity")

        yield vip_mod, session_mod
    finally:
        real_engine.dispose()
        get_settings.cache_clear()


# ==========================================
# 2. Table and Metadata Tests
# ==========================================

def test_vip_capacity_metadata(model_module):
    vip_mod, session_mod = model_module
    model = vip_mod.VipCapacity

    assert issubclass(model, session_mod.Base)
    assert model.__tablename__ == "vip_capacity"

    # Verify Alembic compatibility - table is present in the declarative metadata registry
    assert "vip_capacity" in session_mod.Base.metadata.tables

    table = model.__table__
    expected_columns = {"id", "allocated_count", "created_at", "updated_at"}
    actual_columns = set(table.columns.keys())
    assert expected_columns == actual_columns


# ==========================================
# 3. Column Type and Default Tests
# ==========================================

def test_vip_capacity_columns(model_module):
    vip_mod, _ = model_module
    table = vip_mod.VipCapacity.__table__

    # ID
    id_col = table.columns.id
    assert isinstance(id_col.type, Integer)
    assert id_col.primary_key is True
    assert id_col.nullable is False
    assert id_col.default is None  # Singleton ID must be explicitly set to 1

    # Allocation Counter
    allocated_count_col = table.columns.allocated_count
    assert isinstance(allocated_count_col.type, Integer)
    assert allocated_count_col.nullable is False
    assert allocated_count_col.default is None  # No python-side default
    assert allocated_count_col.server_default is not None
    assert str(allocated_count_col.server_default.arg) == "0"

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


# ==========================================
# 4. Check Constraint Tests
# ==========================================

def test_vip_capacity_check_constraints(model_module):
    vip_mod, _ = model_module
    table = vip_mod.VipCapacity.__table__

    check_constraints = [c for c in table.constraints if isinstance(c, CheckConstraint)]

    expected_checks = {
        "ck_vip_capacity_singleton_id": "id = 1",
        "ck_vip_capacity_allocated_count_bounds": "allocated_count >= 0 AND allocated_count <= 500"
    }

    assert len(check_constraints) == len(expected_checks)

    for constraint in check_constraints:
        assert constraint.name in expected_checks
        assert str(constraint.sqltext) == expected_checks[constraint.name]


# ==========================================
# 5. Primary Key and Index Tests
# ==========================================

def test_vip_capacity_pk_and_indexes(model_module):
    vip_mod, _ = model_module
    table = vip_mod.VipCapacity.__table__

    # Verify primary key contains only the `id` column
    pk_columns = {col.name for col in table.primary_key.columns}
    assert pk_columns == {"id"}

    # Ensure there are no standalone/redundant indexes on this small table
    assert len(table.indexes) == 0


# ==========================================
# 6. Privacy and Representation Tests
# ==========================================

def test_vip_capacity_privacy_repr(model_module):
    vip_mod, _ = model_module

    # Create transient in-memory instance
    capacity = vip_mod.VipCapacity(
        id=1,
        allocated_count=250,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )

    repr_string = repr(capacity)

    # Assert expected properties are visible
    assert "id=1" in repr_string
    assert "allocated_count=250" in repr_string
    assert repr_string == "<VipCapacity(id=1, allocated_count=250)>"
    # Assert there is no other unexpected internal noise
    assert str(capacity.created_at) not in repr_string


# ==========================================
# 7. Alembic Compatibility
# ==========================================

def test_alembic_metadata_compatibility(model_module):
    vip_mod, session_mod = model_module

    # Explicitly confirm the model belongs to the exact same metadata object instance
    assert vip_mod.VipCapacity.metadata is session_mod.Base.metadata

    metadata = session_mod.Base.metadata

    # Confirm it maps to the unified registry cleanly
    assert "vip_capacity" in metadata.tables
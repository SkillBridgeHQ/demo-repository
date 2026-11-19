import base64
import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Session


@pytest.fixture
def session_module(monkeypatch, tmp_path):
    """
    Isolates the environment and dynamically imports the session module
    only AFTER configuring strict test-only environment variables.
    Properly disposes of real SQLAlchemy engines to prevent resource leaks.
    """
    monkeypatch.chdir(tmp_path)

    valid_key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EMAIL_MODE", "console")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db")
    monkeypatch.setenv("ENCRYPTION_KEY_V1", valid_key)

    from app.core.config import get_settings
    get_settings.cache_clear()

    # Dispose of any previously loaded engine before reloading the module
    if "app.db.session" in sys.modules:
        try:
            sys.modules["app.db.session"].engine.dispose()
        except AttributeError:
            pass

    import app.db.session as session_mod

    # Reload ensures it picks up the isolated configuration for this specific test run.
    importlib.reload(session_mod)

    # Keep a reference to the real engine so teardown always cleans up the actual pool,
    # even if a specific test monkeypatches session_mod.engine.
    real_engine = session_mod.engine

    yield session_mod

    real_engine.dispose()
    get_settings.cache_clear()


# ==========================================
# 1. Declarative Base Tests
# ==========================================

def test_declarative_base(session_module):
    """Verifies that Base correctly inherits from SQLAlchemy 2.x DeclarativeBase."""
    assert issubclass(session_module.Base, DeclarativeBase)
    assert isinstance(session_module.Base.metadata, MetaData)


# ==========================================
# 2. Engine Configuration Tests
# ==========================================

def test_engine_configuration(session_module):
    """Verifies the engine uses Psycopg 3 and strict, conservative pooling."""
    engine = session_module.engine

    assert engine.url.drivername == "postgresql+psycopg"

    # Verify connection pool safety configurations
    pool = engine.pool
    assert pool.size() == 5
    assert pool._max_overflow == 5
    assert pool._timeout == 30
    assert pool._recycle == 1800
    assert pool._pre_ping is True


def test_lazy_connection(session_module):
    """
    Verifies that the engine connects lazily.
    Simply importing the module and setting up the engine should not open any connections.
    """
    engine = session_module.engine
    assert engine.pool.checkedout() == 0
    assert engine.pool.checkedin() == 0


# ==========================================
# 3. Session Factory Tests
# ==========================================

def test_session_factory_configuration(session_module):
    """Verifies the synchronous SessionLocal is correctly bound and configured."""
    factory = session_module.SessionLocal

    # sessionmaker creates a subclass of Session internally
    assert issubclass(factory.class_, Session)

    assert factory.kw["bind"] is session_module.engine
    assert factory.kw["autocommit"] is False
    assert factory.kw["autoflush"] is False
    assert factory.kw["expire_on_commit"] is False


# ==========================================
# 4. get_db() Dependency Tests
# ==========================================

def test_get_db_normal_lifecycle(session_module, monkeypatch):
    """Ensures a session is yielded and safely closed upon successful execution."""
    mock_session = MagicMock()
    monkeypatch.setattr(session_module, "SessionLocal", lambda: mock_session)

    gen = session_module.get_db()

    # First yield should provide the db session
    db = next(gen)
    assert db is mock_session

    # Exhausting the generator triggers the finally block
    with pytest.raises(StopIteration):
        next(gen)

    mock_session.rollback.assert_not_called()
    mock_session.close.assert_called_once()


def test_get_db_exception_with_active_transaction(session_module, monkeypatch):
    """Ensures exceptions force a rollback if an active database transaction is in progress."""
    mock_session = MagicMock()
    mock_session.in_transaction.return_value = True
    monkeypatch.setattr(session_module, "SessionLocal", lambda: mock_session)

    gen = session_module.get_db()
    next(gen)

    with pytest.raises(ValueError, match="Mock exception"):
        gen.throw(ValueError("Mock exception"))

    mock_session.in_transaction.assert_called_once()
    mock_session.rollback.assert_called_once()
    mock_session.close.assert_called_once()


def test_get_db_exception_without_active_transaction(session_module, monkeypatch):
    """Ensures exceptions do NOT trigger a rollback if no transaction is active."""
    mock_session = MagicMock()
    mock_session.in_transaction.return_value = False
    monkeypatch.setattr(session_module, "SessionLocal", lambda: mock_session)

    gen = session_module.get_db()
    next(gen)

    with pytest.raises(ValueError, match="Mock exception"):
        gen.throw(ValueError("Mock exception"))

    mock_session.in_transaction.assert_called_once()
    mock_session.rollback.assert_not_called()
    mock_session.close.assert_called_once()


def test_get_db_generator_cleanup(session_module, monkeypatch):
    """Ensures FastAPI's generator closure safely shuts down the connection."""
    mock_session = MagicMock()
    monkeypatch.setattr(session_module, "SessionLocal", lambda: mock_session)

    gen = session_module.get_db()
    next(gen)

    # Close the generator explicitly
    gen.close()

    mock_session.rollback.assert_not_called()
    mock_session.close.assert_called_once()


# ==========================================
# 5. Engine Lifecycle Tests
# ==========================================

def test_dispose_engine(session_module, monkeypatch):
    """Ensures application shutdown triggers engine disposal."""
    mock_engine = MagicMock()
    monkeypatch.setattr(session_module, "engine", mock_engine)

    session_module.dispose_engine()

    mock_engine.dispose.assert_called_once()
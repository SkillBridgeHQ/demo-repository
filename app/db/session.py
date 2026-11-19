from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Retrieve configuration and extract the database URL securely
settings = get_settings()
_db_url = settings.DATABASE_URL.get_secret_value()

# Configure the synchronous SQLAlchemy Engine
# The engine connects lazily; no actual network call is made during import.
engine = create_engine(
    _db_url,
    pool_pre_ping=True,      # Transparently verifies connections before use
    pool_size=5,             # Conservative baseline number of persistent connections
    max_overflow=5,          # Conservative overflow limit
    pool_timeout=30,         # Wait up to 30 seconds for an available connection
    pool_recycle=1800,       # Recycle connections after 30 minutes to prevent staleness
)

# Configure the Session Factory
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    autocommit=False,        # Services must explicitly call db.commit()
    autoflush=False,         # Prevent unexpected flushes during complex read/writes
    expire_on_commit=False,  # Allow access to object attributes after commit
)

class Base(DeclarativeBase):
    """
    SQLAlchemy 2.x Declarative Base.
    All ORM models must inherit from this class.
    Alembic will import Base.metadata to generate database migrations.
    """
    pass

def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency to yield a database session per request.
    Handles transaction rollback safely on exception and guarantees connection closure.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise
    finally:
        db.close()

def dispose_engine() -> None:
    """
    Safely closes all database connections in the pool.
    Intended for use during application shutdown lifecycle events.
    """
    engine.dispose()
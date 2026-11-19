"""Alembic migration environment for SkillBridge Waitlist Backend."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.core.config import get_settings
from app.db.session import Base, engine

# Explicitly import all ORM models so that their tables and constraints
# are registered on the shared Base.metadata object.
from app.models.waitlist_entry import WaitlistEntry  # noqa: F401
from app.models.verification_token import VerificationToken  # noqa: F401
from app.models.vip_capacity import VipCapacity  # noqa: F401
from app.models.email_outbox import EmailOutbox  # noqa: F401


# Alembic Config object, which reads values from alembic.ini.
config = context.config


# Configure logging using the existing alembic.ini configuration.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Use the same metadata instance as the application's ORM models.
target_metadata = Base.metadata


# Expected tables for this backend phase.
_REQUIRED_TABLES = {
    "waitlist_entries",
    "verification_tokens",
    "vip_capacity",
    "email_outbox",
}


def _validate_model_metadata() -> None:
    """
    Validate that all expected ORM tables are registered and that
    foreign-key references can resolve through the shared metadata.
    """
    registered_tables = set(target_metadata.tables.keys())
    missing_tables = _REQUIRED_TABLES - registered_tables

    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        raise RuntimeError(
            f"Alembic model metadata is incomplete. "
            f"Missing ORM tables: {missing}"
        )

    # Resolving each referenced column helps detect missing model imports
    # or unresolved string-based foreign-key references.
    for table in target_metadata.tables.values():
        for foreign_key in table.foreign_keys:
            foreign_key.column


_validate_model_metadata()


def _get_database_url() -> str:
    """
    Return the validated database URL from the application's settings.

    DATABASE_URL is stored as SecretStr, so get_secret_value() is
    required when passing it to SQLAlchemy/Alembic.
    """
    database_url = get_settings().DATABASE_URL.get_secret_value()

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")

    return database_url


def run_migrations_offline() -> None:
    """
    Run migrations without creating a live database connection.

    The database URL is passed directly to Alembic's migration context.
    This avoids ConfigParser interpolation issues with special
    characters such as '%' in PostgreSQL passwords.
    """
    database_url = _get_database_url()

    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=False,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations using the application's existing synchronous engine.

    No second SQLAlchemy engine is created. The connection is managed
    by a context manager and the engine is disposed after the standalone
    Alembic CLI operation completes.
    """
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=False,
                transaction_per_migration=True,
            )

            with context.begin_transaction():
                context.run_migrations()

    finally:
        # The connection context has exited before engine disposal.
        # Alembic runs as a standalone CLI process, so disposing this
        # process-local engine is appropriate.
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
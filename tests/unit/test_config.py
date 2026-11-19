import base64
import os
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """
    Prevents loading the project's real .env file by changing to a temporary directory.
    Provides a valid baseline development environment for isolated testing.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONSENT_VERSION", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    valid_key = base64.b64encode(os.urandom(32)).decode("ascii")

    # Baseline Development Environment
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test_user:test_pass@localhost:5432/test_db")
    monkeypatch.setenv("ENCRYPTION_KEY_V1", valid_key)
    monkeypatch.setenv("EMAIL_MODE", "console")

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# ==========================================
# A. Basic Settings & Caching
# ==========================================

def test_valid_development_config():
    """Ensures the baseline development configuration loads without errors."""
    settings = get_settings()
    assert settings.APP_ENV == "development"
    assert settings.EMAIL_MODE == "console"
    assert isinstance(settings.DATABASE_URL, SecretStr)
    assert isinstance(settings.ENCRYPTION_KEY_V1, SecretStr)
    assert settings.ENCRYPTION_ACTIVE_KEY_VERSION == "v1"


def test_get_settings_caching():
    """Ensures get_settings uses lru_cache correctly to avoid re-parsing env vars."""
    settings_one = get_settings()
    settings_two = get_settings()
    assert settings_one is settings_two

    get_settings.cache_clear()
    settings_three = get_settings()
    assert settings_one is not settings_three


def test_missing_required_settings(isolated_env):
    """Ensures standard Pydantic validation catches missing mandatory fields."""
    isolated_env.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()

    isolated_env.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/db")
    isolated_env.delenv("ENCRYPTION_KEY_V1", raising=False)
    with pytest.raises(ValidationError):
        Settings()


# ==========================================
# B. Database URL Validation
# ==========================================

@pytest.mark.parametrize("bad_url, expected_error", [
    ("mysql://localhost/db", r"strictly use the postgresql\+psycopg driver"),
    ("not_a_url", "malformed"),
    ("postgresql+psycopg://", "contain a valid hostname"),
    ("postgresql+psycopg://localhost/", "contain a valid database name"),
])
def test_database_url_invalid(isolated_env, bad_url, expected_error):
    isolated_env.setenv("DATABASE_URL", bad_url)
    with pytest.raises(ValidationError, match=expected_error):
        Settings()


# ==========================================
# C. Encryption Configuration
# ==========================================

@pytest.mark.parametrize("bad_key, expected_error", [
    ("not_base64_!@#$", "valid Base64 encoded string"),
    (base64.b64encode(os.urandom(31)).decode("ascii"), "exactly 32 bytes for AES-256-GCM"),
    (base64.b64encode(os.urandom(33)).decode("ascii"), "exactly 32 bytes for AES-256-GCM"),
])
def test_encryption_key_invalid(isolated_env, bad_key, expected_error):
    isolated_env.setenv("ENCRYPTION_KEY_V1", bad_key)
    with pytest.raises(ValidationError, match=expected_error):
        Settings()


def test_encryption_version_lock(isolated_env):
    """Ensures the application strictly requires v1 until dynamic rotation is implemented."""
    isolated_env.setenv("ENCRYPTION_ACTIVE_KEY_VERSION", "v2")
    with pytest.raises(ValidationError, match="must equal 'v1'"):
        Settings()


# ==========================================
# D. CORS Validation
# ==========================================

def test_cors_normalization(isolated_env):
    """Tests that default ports are correctly stripped and hosts lowercased."""
    isolated_env.setenv(
        "CORS_ORIGINS",
        '["http://EXAMPLE.com:80", "https://EXAMPLE.com:443", "https://example.com:8443"]'
    )
    settings = Settings()
    assert "http://example.com" in settings.CORS_ORIGINS
    assert "https://example.com" in settings.CORS_ORIGINS
    assert "https://example.com:8443" in settings.CORS_ORIGINS


@pytest.mark.parametrize("bad_origin, expected_error", [
    ("http://localhost:5173/", "must not contain a trailing slash"),
    ("ftp://localhost", "http or https scheme"),
    ("https://user:pass@localhost", "must not contain credentials"),
    ("https://localhost/path", "must not contain a path"),
    ("https://localhost?q=1", "query parameters or fragments"),
    ("https://localhost#frag", "query parameters or fragments"),
    ("https://localhost:99999", "invalid port number"),
    ("https://localhost:abc", "invalid port number"),
    ("https://localhost:", "malformed empty port"),
])
def test_cors_origins_invalid(isolated_env, bad_origin, expected_error):
    isolated_env.setenv("CORS_ORIGINS", f'["{bad_origin}"]')
    with pytest.raises(ValidationError, match=expected_error):
        Settings()


# ==========================================
# E. Trusted Proxy Validation
# ==========================================

def test_proxy_cidrs_valid_ipv4_and_ipv6(isolated_env):
    """Tests that legitimate IPs and networks are successfully parsed."""
    isolated_env.setenv("TRUSTED_PROXY_CIDRS", '["192.168.1.1/32", "10.0.0.0/8", "2001:db8::/32"]')
    settings = Settings()
    assert len(settings.TRUSTED_PROXY_CIDRS) == 3


@pytest.mark.parametrize("bad_cidr", [
    "not_an_ip",
    "256.256.256.256/32",
    "127.0.0.1/33"
])
def test_proxy_cidrs_invalid(isolated_env, bad_cidr):
    isolated_env.setenv("TRUSTED_PROXY_CIDRS", f'["{bad_cidr}"]')
    with pytest.raises(ValidationError):
        Settings()


# ==========================================
# F. Public Frontend URL
# ==========================================

def test_frontend_url_normalization(isolated_env):
    """Tests URL normalization and preservation of paths (stripping trailing slash)."""
    isolated_env.setenv("PUBLIC_FRONTEND_URL", "HTTPS://EXAMPLE.COM:443/app/")
    settings = Settings()
    assert settings.PUBLIC_FRONTEND_URL == "https://example.com/app"


@pytest.mark.parametrize("bad_url, expected_error", [
    ("http://localhost:5173/?q=1", "query parameters or fragments"),
    ("http://localhost:5173/#frag", "query parameters or fragments"),
    ("https://localhost:", "malformed empty port"),
    ("https://user:pass@localhost", "must not contain credentials"),
    ("ftp://localhost", "http or https scheme"),
    ("https://localhost:99999", "invalid port number"),
    ("https://localhost:abc", "invalid port number"),
])
def test_frontend_url_invalid(isolated_env, bad_url, expected_error):
    isolated_env.setenv("PUBLIC_FRONTEND_URL", bad_url)
    with pytest.raises(ValidationError, match=expected_error):
        Settings()


# ==========================================
# G. Email Configuration
# ==========================================

def test_email_mode_console_ignores_missing_sender(isolated_env):
    """Ensures development boots successfully without email provider configuration."""
    isolated_env.delenv("EMAIL_FROM", raising=False)
    isolated_env.delenv("RESEND_API_KEY", raising=False)
    settings = Settings()
    assert settings.EMAIL_MODE == "console"
    assert settings.EMAIL_FROM is None


@pytest.mark.parametrize("bad_email", [
    "not_an_email",
    "user@domain\n.com",
    "SkillBridge Waitlist\r\n <waitlist@domain.com>"
])
def test_email_from_invalid(isolated_env, bad_email):
    isolated_env.setenv("EMAIL_MODE", "resend")
    isolated_env.setenv("RESEND_API_KEY", "test_key")
    isolated_env.setenv("EMAIL_FROM", bad_email)
    with pytest.raises(ValidationError):
        Settings()


def test_email_resend_mode_requires_key_and_sender(isolated_env):
    isolated_env.setenv("EMAIL_MODE", "resend")
    isolated_env.delenv("RESEND_API_KEY", raising=False)
    isolated_env.setenv("EMAIL_FROM", "Waitlist <valid@test.com>")
    with pytest.raises(ValidationError, match="RESEND_API_KEY must be present"):
        Settings()

    isolated_env.setenv("RESEND_API_KEY", "test_key")
    isolated_env.delenv("EMAIL_FROM", raising=False)
    with pytest.raises(ValidationError, match="EMAIL_FROM must be explicitly configured"):
        Settings()


# ==========================================

def test_email_smtp_mode_accepts_valid_gmail_settings(isolated_env):
    isolated_env.setenv("EMAIL_MODE", "smtp")
    isolated_env.setenv("SMTP_USERNAME", "team.skillbridgehq.in@gmail.com")
    isolated_env.setenv("SMTP_PASSWORD", "test-only-password")
    isolated_env.setenv(
        "EMAIL_FROM",
        "SkillBridge Waitlist <team.skillbridgehq.in@gmail.com>",
    )

    settings = Settings()

    assert settings.EMAIL_MODE == "smtp"
    assert settings.SMTP_HOST == "smtp.gmail.com"
    assert settings.SMTP_PORT == 587
    assert settings.SMTP_USERNAME == "team.skillbridgehq.in@gmail.com"
    assert settings.SMTP_PASSWORD.get_secret_value() == "test-only-password"


def test_email_smtp_mode_requires_username(isolated_env):
    isolated_env.setenv("EMAIL_MODE", "smtp")
    isolated_env.delenv("SMTP_USERNAME", raising=False)
    isolated_env.setenv("SMTP_PASSWORD", "test-only-password")
    isolated_env.setenv("EMAIL_FROM", "team.skillbridgehq.in@gmail.com")

    with pytest.raises(ValidationError, match="SMTP_USERNAME must be configured"):
        Settings()


def test_email_smtp_mode_requires_password(isolated_env):
    isolated_env.setenv("EMAIL_MODE", "smtp")
    isolated_env.setenv("SMTP_USERNAME", "team.skillbridgehq.in@gmail.com")
    isolated_env.delenv("SMTP_PASSWORD", raising=False)
    isolated_env.setenv("EMAIL_FROM", "team.skillbridgehq.in@gmail.com")

    with pytest.raises(ValidationError, match="SMTP_PASSWORD must be present"):
        Settings()


def test_email_smtp_rejects_non_starttls_port(isolated_env):
    isolated_env.setenv("EMAIL_MODE", "smtp")
    isolated_env.setenv("SMTP_USERNAME", "team.skillbridgehq.in@gmail.com")
    isolated_env.setenv("SMTP_PASSWORD", "test-only-password")
    isolated_env.setenv("SMTP_PORT", "465")
    isolated_env.setenv("EMAIL_FROM", "team.skillbridgehq.in@gmail.com")

    with pytest.raises(
        ValidationError,
        match="SMTP_PORT must be 587 when using STARTTLS",
    ):
        Settings()

def test_email_smtp_sender_must_match_username(isolated_env):
    isolated_env.setenv("EMAIL_MODE", "smtp")
    isolated_env.setenv("SMTP_USERNAME", "team.skillbridgehq.in@gmail.com")
    isolated_env.setenv("SMTP_PASSWORD", "test-only-password")
    isolated_env.setenv("EMAIL_FROM", "other@example.org")

    with pytest.raises(ValidationError, match="must match SMTP_USERNAME"):
        Settings()


def test_production_smtp_config_is_accepted(prod_env):
    prod_env.setenv("EMAIL_MODE", "smtp")
    prod_env.delenv("RESEND_API_KEY", raising=False)
    prod_env.setenv("SMTP_USERNAME", "team.skillbridgehq.in@gmail.com")
    prod_env.setenv("SMTP_PASSWORD", "test-only-password")
    prod_env.setenv(
        "EMAIL_FROM",
        "SkillBridge Waitlist <team.skillbridgehq.in@gmail.com>",
    )

    settings = Settings()

    assert settings.APP_ENV == "production"
    assert settings.EMAIL_MODE == "smtp"

# H. Production Security Validation
# ==========================================

@pytest.fixture
def prod_env(isolated_env):
    """Applies a strict, completely valid configuration baseline for a production environment."""
    isolated_env.setenv("APP_ENV", "production")
    isolated_env.setenv("DEBUG", "False")
    isolated_env.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.local:5432/db?sslmode=require")
    isolated_env.setenv("EMAIL_MODE", "resend")
    isolated_env.setenv("RESEND_API_KEY", "prod_key_12345")
    isolated_env.setenv("EMAIL_FROM", "SkillBridge Waitlist <waitlist@skillbridge.dev>")
    isolated_env.setenv("PUBLIC_FRONTEND_URL", "https://skillbridge.dev")
    isolated_env.setenv("CORS_ORIGINS", '["https://skillbridge.dev"]')
    isolated_env.setenv("TRUSTED_PROXY_CIDRS", '["10.0.0.0/8"]')
    isolated_env.setenv("EARLY_ACCESS_CAP", "500")
    isolated_env.setenv("REDIS_URL", "rediss://user:password@redis.example.com:6380/0")
    isolated_env.setenv("CONSENT_VERSION", "test-consent-v1")
    return isolated_env


def test_production_valid_config(prod_env):
    """Proves the production baseline is completely valid before negative testing."""
    settings = Settings()
    assert settings.APP_ENV == "production"


@pytest.mark.parametrize("env_var, bad_val, expected_error", [
    ("DEBUG", "True", "cannot use DEBUG=True"),
    ("EARLY_ACCESS_CAP", "499", "exactly 500 in production"),
    ("EMAIL_MODE", "console", "must be 'resend' or 'smtp' in production"),
    ("DATABASE_URL", "postgresql+psycopg://u:p@db.local/db", "sslmode=require"),
    ("PUBLIC_FRONTEND_URL", "http://skillbridge.dev", "must use HTTPS in production"),
    ("PUBLIC_FRONTEND_URL", "https://localhost", "Localhost PUBLIC_FRONTEND_URL is prohibited"),
    ("PUBLIC_FRONTEND_URL", "https://test.localhost", "Localhost PUBLIC_FRONTEND_URL is prohibited"),
    ("PUBLIC_FRONTEND_URL", "https://127.0.0.1", "Loopback or unspecified PUBLIC_FRONTEND_URL"),
    ("PUBLIC_FRONTEND_URL", "https://[::1]", "Loopback or unspecified PUBLIC_FRONTEND_URL"),
    ("PUBLIC_FRONTEND_URL", "https://0.0.0.0", "Loopback or unspecified PUBLIC_FRONTEND_URL"),
    ("PUBLIC_FRONTEND_URL", "https://[::]", "Loopback or unspecified PUBLIC_FRONTEND_URL"),
    ("CORS_ORIGINS", '["http://skillbridge.dev"]', "must use HTTPS"),
    ("CORS_ORIGINS", '["https://localhost"]', "Localhost CORS origin"),
    ("CORS_ORIGINS", '["https://test.localhost"]', "Localhost CORS origin"),
    ("CORS_ORIGINS", '["https://127.0.0.1"]', "Loopback or unspecified CORS origin"),
    ("CORS_ORIGINS", '["https://[::1]"]', "Loopback or unspecified CORS origin"),
    ("CORS_ORIGINS", '["https://0.0.0.0"]', "Loopback or unspecified CORS origin"),
    ("CORS_ORIGINS", '["https://[::]"]', "Loopback or unspecified CORS origin"),
    ("CORS_ORIGINS", '["*"]', "Wildcard CORS"),
    ("TRUSTED_PROXY_CIDRS", '["0.0.0.0/0"]', "Unrestricted proxy networks"),
    ("TRUSTED_PROXY_CIDRS", '["::/0"]', "Unrestricted proxy networks"),
    ("EMAIL_FROM", "test@skillbridge.local", "invalid email address"),
    ("EMAIL_FROM", "test@skillbridge.test", "invalid email address"),
    ("EMAIL_FROM", "test@example.com", "local or placeholder domain"),
])
def test_production_invalid_configs(prod_env, env_var, bad_val, expected_error):
    """
    Mutates exactly one value in the valid production environment to prove
    each individual security tripwire works flawlessly.
    """
    prod_env.setenv(env_var, bad_val)
    with pytest.raises(ValidationError, match=expected_error):
        Settings()
# ==========================================
# CONSENT_VERSION Regression Tests
# ==========================================

def test_consent_version_missing_in_development(isolated_env):
    # Development accepts missing value and returns None
    from app.core.config import get_settings
    settings = get_settings()
    assert settings.CONSENT_VERSION is None

def test_consent_version_valid(isolated_env):
    isolated_env.setenv("CONSENT_VERSION", "test-consent-v1")
    from app.core.config import get_settings
    settings = get_settings()
    assert settings.CONSENT_VERSION == "test-consent-v1"

def test_consent_version_strips_whitespace(isolated_env):
    isolated_env.setenv("CONSENT_VERSION", "  test-consent-v2  \n")
    from app.core.config import get_settings
    settings = get_settings()
    assert settings.CONSENT_VERSION == "test-consent-v2"

@pytest.mark.parametrize("invalid_value", [
    "",
    "   ",
    "\n",
    "\t"
])
def test_consent_version_rejects_blank(isolated_env, invalid_value):
    isolated_env.setenv("CONSENT_VERSION", invalid_value)
    from app.core.config import get_settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert "CONSENT_VERSION cannot be empty or whitespace-only" in str(exc.value)

def test_consent_version_length_boundaries(isolated_env):
    from app.core.config import get_settings
    from pydantic import ValidationError

    # Exactly 50 characters - Accepted
    valid_50 = "v" * 50
    isolated_env.setenv("CONSENT_VERSION", valid_50)
    settings = get_settings()
    assert settings.CONSENT_VERSION == valid_50

    # 51 characters - Rejected
    get_settings.cache_clear()
    invalid_51 = "v" * 51
    isolated_env.setenv("CONSENT_VERSION", invalid_51)

    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert "CONSENT_VERSION must not exceed 50 characters." in str(exc.value)

def test_production_rejects_missing_consent_version(prod_env):
    # prod_env already sets a valid CONSENT_VERSION. Remove it.
    prod_env.delenv("CONSENT_VERSION", raising=False)

    from app.core.config import get_settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert "CONSENT_VERSION must be explicitly configured in production" in str(exc.value)

def test_production_rejects_blank_consent_version(prod_env):
    # Overwrite the valid production fixture value with a blank string
    prod_env.setenv("CONSENT_VERSION", "   ")

    from app.core.config import get_settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert "CONSENT_VERSION cannot be empty or whitespace-only" in str(exc.value)

# REDIS URL configuration regression tests

def test_redis_url_optional_in_development(isolated_env):
    settings = Settings()
    assert settings.REDIS_URL is None


def test_redis_url_valid_development(isolated_env):
    redis_url = "redis://user:redis-secret@localhost:6379/0"
    isolated_env.setenv("REDIS_URL", redis_url)

    settings = Settings()

    assert settings.REDIS_URL.get_secret_value() == redis_url
    assert "redis-secret" not in repr(settings)


@pytest.mark.parametrize(
    "redis_url",
    [
        "https://localhost",
        "redis:///0",
        "redis://localhost:99999/0",
        "redis://localhost/not-a-number",
        "redis://localhost:6379/0?unexpected=1",
        "redis://localhost:/0",
    ],
)
def test_redis_url_rejects_invalid_values(isolated_env, redis_url):
    isolated_env.setenv("REDIS_URL", redis_url)

    with pytest.raises(ValidationError, match="REDIS_URL"):
        Settings()


def test_production_rejects_missing_redis_url(prod_env):
    prod_env.delenv("REDIS_URL", raising=False)

    with pytest.raises(
        ValidationError,
        match="REDIS_URL must be explicitly configured in production",
    ):
        Settings()


def test_production_rejects_non_tls_redis_url(prod_env):
    prod_env.setenv("REDIS_URL", "redis://redis.example.com:6379/0")

    with pytest.raises(
        ValidationError,
        match="REDIS_URL must use rediss",
    ):
        Settings()

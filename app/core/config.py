import base64
import binascii
import ipaddress
from email.utils import parseaddr
from functools import lru_cache
from typing import Any, List, Literal, Optional
from urllib.parse import urlparse

from email_validator import EmailNotValidError, validate_email
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    # 1. Application
    APP_NAME: str = "SkillBridge Waitlist API"
    APP_ENV: Literal["development", "test", "production"] = "development"
    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # 2. Database
    DATABASE_URL: SecretStr

    # 3. CORS
    CORS_ORIGINS: List[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # 4. Security
    ENCRYPTION_KEY_V1: Optional[SecretStr] = None
    ENCRYPTION_KEY_V2: Optional[SecretStr] = None
    ENCRYPTION_ACTIVE_KEY_VERSION: Literal["v1", "v2"] = "v1"
    TRUSTED_PROXY_CIDRS: List[str] = Field(
        default_factory=lambda: ["127.0.0.1/32"]
    )

    # 5. Distributed Rate Limiting
    REDIS_URL: Optional[SecretStr] = None

    # 5. Email
    EMAIL_MODE: Literal["console", "resend", "smtp"] = "console"
    RESEND_API_KEY: Optional[SecretStr] = None
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[SecretStr] = None
    EMAIL_FROM: Optional[str] = None
    PUBLIC_FRONTEND_URL: str = "http://localhost:5173"

    # 6. Waitlist
    EARLY_ACCESS_CAP: int = Field(default=500, ge=1)

    # Must match the actual approved consent document version.
    # No hardcoded default is trusted.
    CONSENT_VERSION: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=50,
        description="Authoritative server-side consent document version."
    )

    # 7. Verification
    VERIFICATION_TOKEN_TTL_MINUTES: int = Field(default=1440, ge=1, le=10080)
    DELETION_TOKEN_TTL_MINUTES: int = Field(default=60, ge=1, le=1440)

    # 8. Email Worker
    OUTBOX_POLL_INTERVAL_SECONDS: int = Field(
        default=5, ge=1, le=300,
        description="Seconds between empty or partial outbox batches."
    )
    OUTBOX_LEASE_SECONDS: int = Field(
        default=1800, ge=60, le=3600,
        description="Maximum duration of an outbox processing lease."
    )
    OUTBOX_MAX_RETRIES: int = Field(
        default=5, ge=1, le=20,
        description="Maximum total delivery attempts, including the initial attempt."
    )
    OUTBOX_BACKOFF_BASE_SECONDS: int = Field(
        default=60, ge=1, le=3600,
        description="Base delay for exponential retry scheduling."
    )

    # 9. Rate Limiting (requests per minute)
    REGISTRATION_RATE_LIMIT: int = Field(default=5, ge=1, le=100)
    VERIFICATION_RATE_LIMIT: int = Field(default=10, ge=1, le=200)
    RESEND_RATE_LIMIT: int = Field(default=3, ge=1, le=20)
    RESEND_EMAIL_COOLDOWN_SECONDS: int = Field(default=300, ge=60, le=3600)
    DELETION_RATE_LIMIT: int = Field(default=3, ge=1, le=20)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def validate_redis_url(cls, value: Any) -> Any:
        """Validate Redis connection URLs without exposing credentials."""
        if value is None:
            return None

        if isinstance(value, SecretStr):
            value = value.get_secret_value()

        if not isinstance(value, str):
            raise ValueError("REDIS_URL must be a valid Redis URL.")

        cleaned = value.strip()

        if not cleaned or any(character.isspace() for character in cleaned):
            raise ValueError("REDIS_URL must be a non-empty URL without whitespace.")

        parsed = urlparse(cleaned)

        if parsed.scheme not in ("redis", "rediss"):
            raise ValueError("REDIS_URL must use redis:// or rediss://.")

        try:
            hostname = parsed.hostname
        except ValueError:
            raise ValueError("REDIS_URL contains a malformed hostname.") from None

        if not hostname:
            raise ValueError("REDIS_URL must contain a valid hostname.")

        if parsed.netloc.endswith(":"):
            raise ValueError("REDIS_URL contains a malformed empty port.")

        try:
            port = parsed.port
        except ValueError:
            raise ValueError("REDIS_URL contains an invalid port number.") from None

        if port is not None and not (1 <= port <= 65535):
            raise ValueError("REDIS_URL contains an out-of-range port number.")

        if parsed.query or parsed.fragment:
            raise ValueError("REDIS_URL must not contain query parameters or fragments.")

        if parsed.path not in ("", "/") and not (
            parsed.path.startswith("/") and parsed.path[1:].isdigit()
        ):
            raise ValueError("REDIS_URL contains an invalid database path.")

        return cleaned
    @field_validator("CONSENT_VERSION", mode="before")
    @classmethod
    def validate_consent_version(cls, value: Any) -> Any:
        """
        Normalize the consent version and reject explicitly blank values.

        Missing values remain None in development/testing.
        Production requirements are enforced by the model validator.
        """
        if value is None:
            return None

        if isinstance(value, str):
            cleaned = value.strip()

            if not cleaned:
                raise ValueError(
                    "CONSENT_VERSION cannot be empty or whitespace-only."
                )

            if len(cleaned) > 50:
                raise ValueError(
                    "CONSENT_VERSION must not exceed 50 characters."
                )

            return cleaned

        return value

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        # 1. Database URL Validation
        try:
            parsed_db_url = make_url(self.DATABASE_URL.get_secret_value())
        except ArgumentError:
            raise ValueError("DATABASE_URL is malformed.")

        if parsed_db_url.drivername != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must strictly use the postgresql+psycopg driver.")
        if not parsed_db_url.host:
            raise ValueError("DATABASE_URL must contain a valid hostname.")
        if not parsed_db_url.database:
            raise ValueError("DATABASE_URL must contain a valid database name.")

        # 2. Versioned AES-256-GCM encryption key validation.
        encryption_keys = {
            "v1": self.ENCRYPTION_KEY_V1,
            "v2": self.ENCRYPTION_KEY_V2,
        }

        active_key = encryption_keys[self.ENCRYPTION_ACTIVE_KEY_VERSION]
        if active_key is None or not active_key.get_secret_value().strip():
            raise ValueError(
                f"ENCRYPTION_KEY_{self.ENCRYPTION_ACTIVE_KEY_VERSION.upper()} "
                "must be configured for the active encryption version."
            )

        for version, secret in encryption_keys.items():
            if secret is None:
                continue

            key_name = f"ENCRYPTION_KEY_{version.upper()}"
            try:
                key_bytes = base64.b64decode(
                    secret.get_secret_value(),
                    validate=True,
                )
            except (binascii.Error, ValueError, UnicodeError):
                raise ValueError(
                    f"{key_name} must be a valid Base64 encoded string."
                ) from None

            if len(key_bytes) != 32:
                raise ValueError(
                    f"{key_name} must decode to exactly 32 bytes for AES-256-GCM."
                )

        # 3. Trusted Proxy Validation
        for cidr in self.TRUSTED_PROXY_CIDRS:
            try:
                network = ipaddress.ip_network(cidr, strict=False)

                if self.APP_ENV == "production" and network.prefixlen == 0:
                    raise ValueError(
                        f"Unrestricted proxy networks ({cidr}) are strictly prohibited in production."
                    )

            except ValueError as e:
                if "Unrestricted" in str(e):
                    raise
                raise ValueError(
                    f"Invalid network or IP address in TRUSTED_PROXY_CIDRS: {cidr}"
                )

        # 4. CORS Validation and Normalization
        normalized_origins = []

        for origin in self.CORS_ORIGINS:
            if origin == "*":
                if self.APP_ENV == "production":
                    raise ValueError(
                        "Wildcard CORS ('*') is strictly prohibited in production."
                    )

                normalized_origins.append(origin)
                continue

            if origin.endswith("/"):
                raise ValueError(
                    f"CORS origin '{origin}' must not contain a trailing slash."
                )

            parsed_origin = urlparse(origin)

            if parsed_origin.scheme not in ("http", "https"):
                raise ValueError(
                    f"CORS origin '{origin}' must use http or https scheme."
                )

            if parsed_origin.username or parsed_origin.password:
                raise ValueError(
                    f"CORS origin '{origin}' must not contain credentials."
                )

            if parsed_origin.path and parsed_origin.path != "":
                raise ValueError(
                    f"CORS origin '{origin}' must not contain a path."
                )

            if parsed_origin.query or parsed_origin.fragment:
                raise ValueError(
                    f"CORS origin '{origin}' must not contain query parameters or fragments."
                )

            if parsed_origin.netloc.endswith(":"):
                raise ValueError(
                    f"CORS origin '{origin}' contains a malformed empty port."
                )

            try:
                origin_port = parsed_origin.port
            except ValueError:
                raise ValueError(
                    f"CORS origin '{origin}' contains an invalid port number."
                )

            if origin_port is not None and not (1 <= origin_port <= 65535):
                raise ValueError(
                    f"CORS origin '{origin}' contains an out-of-range port number."
                )

            origin_host = parsed_origin.hostname

            if not origin_host:
                raise ValueError(f"CORS origin '{origin}' is malformed.")

            is_ip = False
            ip_obj = None

            try:
                ip_obj = ipaddress.ip_address(origin_host)
                is_ip = True
            except ValueError:
                pass

            if self.APP_ENV == "production":
                if parsed_origin.scheme != "https":
                    raise ValueError(
                        f"Production CORS origin '{origin}' must use HTTPS."
                    )

                if is_ip:
                    if ip_obj.is_loopback or ip_obj.is_unspecified:
                        raise ValueError(
                            f"Loopback or unspecified CORS origin '{origin}' is prohibited in production."
                        )
                else:
                    host_lower = origin_host.lower()

                    if host_lower == "localhost" or host_lower.endswith(".localhost"):
                        raise ValueError(
                            f"Localhost CORS origin '{origin}' is prohibited in production."
                        )

            formatted_host = origin_host.lower()

            if is_ip and ip_obj.version == 6:
                formatted_host = f"[{formatted_host}]"

            normalized_origin = f"{parsed_origin.scheme}://{formatted_host}"

            if origin_port is not None:
                if not (
                    (parsed_origin.scheme == "http" and origin_port == 80)
                    or
                    (parsed_origin.scheme == "https" and origin_port == 443)
                ):
                    normalized_origin += f":{origin_port}"

            normalized_origins.append(normalized_origin)

        self.CORS_ORIGINS = normalized_origins

        # 5. Frontend URL Validation and Normalization
        parsed_frontend = urlparse(self.PUBLIC_FRONTEND_URL)

        if parsed_frontend.scheme not in ("http", "https"):
            raise ValueError("PUBLIC_FRONTEND_URL must use http or https scheme.")

        if parsed_frontend.username or parsed_frontend.password:
            raise ValueError("PUBLIC_FRONTEND_URL must not contain credentials.")

        if parsed_frontend.query or parsed_frontend.fragment:
            raise ValueError("PUBLIC_FRONTEND_URL must not contain query parameters or fragments.")

        if parsed_frontend.netloc.endswith(":"):
            raise ValueError("PUBLIC_FRONTEND_URL contains a malformed empty port.")

        try:
            frontend_port = parsed_frontend.port
        except ValueError:
            raise ValueError("PUBLIC_FRONTEND_URL contains an invalid port number.")

        if frontend_port is not None and not (1 <= frontend_port <= 65535):
            raise ValueError("PUBLIC_FRONTEND_URL contains an out-of-range port number.")

        frontend_host = parsed_frontend.hostname

        if not frontend_host:
            raise ValueError("PUBLIC_FRONTEND_URL is malformed.")

        is_ip = False
        ip_obj = None

        try:
            ip_obj = ipaddress.ip_address(frontend_host)
            is_ip = True
        except ValueError:
            pass

        if self.APP_ENV == "production":
            if parsed_frontend.scheme != "https":
                raise ValueError("PUBLIC_FRONTEND_URL must use HTTPS in production.")

            if is_ip:
                if ip_obj.is_loopback or ip_obj.is_unspecified:
                    raise ValueError(
                        "Loopback or unspecified PUBLIC_FRONTEND_URL is prohibited in production."
                    )
            else:
                host_lower = frontend_host.lower()

                if host_lower == "localhost" or host_lower.endswith(".localhost"):
                    raise ValueError(
                        "Localhost PUBLIC_FRONTEND_URL is prohibited in production."
                    )

        formatted_frontend_host = frontend_host.lower()

        if is_ip and ip_obj.version == 6:
            formatted_frontend_host = f"[{formatted_frontend_host}]"

        normalized_frontend = f"{parsed_frontend.scheme}://{formatted_frontend_host}"

        if frontend_port is not None:
            if not (
                (parsed_frontend.scheme == "http" and frontend_port == 80)
                or
                (parsed_frontend.scheme == "https" and frontend_port == 443)
            ):
                normalized_frontend += f":{frontend_port}"

        if parsed_frontend.path and parsed_frontend.path != "/":
            normalized_frontend += parsed_frontend.path.rstrip("/")

        self.PUBLIC_FRONTEND_URL = normalized_frontend

        # 6. Email Sender and Mode Validation
        normalized_email_domain = None

        if self.EMAIL_MODE == "resend":
            if (
                not self.RESEND_API_KEY
                or not self.RESEND_API_KEY.get_secret_value().strip()
            ):
                raise ValueError(
                    "RESEND_API_KEY must be present and non-empty when EMAIL_MODE is 'resend'."
                )

        elif self.EMAIL_MODE == "smtp":
            if not isinstance(self.SMTP_USERNAME, str) or not self.SMTP_USERNAME.strip():
                raise ValueError(
                    "SMTP_USERNAME must be configured when EMAIL_MODE is 'smtp'."
                )

            if (
                not self.SMTP_PASSWORD
                or not self.SMTP_PASSWORD.get_secret_value().strip()
            ):
                raise ValueError(
                    "SMTP_PASSWORD must be present and non-empty when EMAIL_MODE is 'smtp'."
                )

            if (
                not isinstance(self.SMTP_HOST, str)
                or not self.SMTP_HOST.strip()
                or "\n" in self.SMTP_HOST
                or "\r" in self.SMTP_HOST
            ):
                raise ValueError("SMTP_HOST must be a valid non-empty hostname.")

            if self.SMTP_PORT != 587:
                raise ValueError(
                    "SMTP_PORT must be 587 when using STARTTLS."
                )

            if "\n" in self.SMTP_USERNAME or "\r" in self.SMTP_USERNAME:
                raise ValueError("SMTP_USERNAME must not contain newline characters.")

            try:
                normalized_smtp_username = validate_email(
                    self.SMTP_USERNAME,
                    check_deliverability=False,
                ).normalized
            except EmailNotValidError as exc:
                raise ValueError("SMTP_USERNAME must be a valid email address.") from exc

        if self.EMAIL_MODE in ("resend", "smtp"):
            if not self.EMAIL_FROM or not self.EMAIL_FROM.strip():
                raise ValueError(
                    f"EMAIL_FROM must be explicitly configured when EMAIL_MODE is '{self.EMAIL_MODE}'."
                )

            if "\n" in self.EMAIL_FROM or "\r" in self.EMAIL_FROM:
                raise ValueError(
                    "EMAIL_FROM must not contain CRLF or newline characters."
                )

            display_name, email_address = parseaddr(self.EMAIL_FROM)

            if not email_address:
                raise ValueError("EMAIL_FROM must contain a valid email address.")

            try:
                valid_email = validate_email(
                    email_address,
                    check_deliverability=False,
                )
                normalized_email = valid_email.normalized
                normalized_email_domain = normalized_email.split("@", 1)[1]
            except EmailNotValidError as exc:
                raise ValueError(
                    f"EMAIL_FROM contains an invalid email address: {exc}"
                ) from exc

            if (
                self.EMAIL_MODE == "smtp"
                and normalized_email != normalized_smtp_username
            ):
                raise ValueError(
                    "For SMTP, the EMAIL_FROM address must match SMTP_USERNAME."
                )

        elif self.EMAIL_FROM:
            if "\n" in self.EMAIL_FROM or "\r" in self.EMAIL_FROM:
                raise ValueError(
                    "EMAIL_FROM must not contain CRLF or newline characters."
                )

        # 7. Production Constraints
        if self.APP_ENV == "production":
            if self.EARLY_ACCESS_CAP != 500:
                raise ValueError(
                    "EARLY_ACCESS_CAP must be exactly 500 in production."
                )

            if self.DEBUG:
                raise ValueError(
                    "APP_ENV=production cannot use DEBUG=True."
                )

            if self.EMAIL_MODE not in ("resend", "smtp"):
                raise ValueError(
                    "EMAIL_MODE must be 'resend' or 'smtp' in production."
                )

            if parsed_db_url.query.get("sslmode") not in (
                "require",
                "verify-ca",
                "verify-full"
            ):
                raise ValueError(
                    "DATABASE_URL must enforce secure transport (sslmode=require/verify-ca/verify-full) in production."
                )

            if normalized_email_domain:
                if (
                    normalized_email_domain.endswith((".local", ".test"))
                    or normalized_email_domain == "example.com"
                ):
                    raise ValueError(
                        "EMAIL_FROM must not use a local or placeholder domain in production."
                    )

            # Trusted consent document version is mandatory in production.
            if not self.CONSENT_VERSION:
                raise ValueError(
                    "CONSENT_VERSION must be explicitly configured in production."
                )
            if not self.REDIS_URL:
                raise ValueError(
                    "REDIS_URL must be explicitly configured in production."
                )

            redis_url = self.REDIS_URL.get_secret_value()
            if urlparse(redis_url).scheme != "rediss":
                raise ValueError(
                    "REDIS_URL must use rediss:// with TLS in production."
                )

        return self


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached instance of the application settings.
    Usage: settings = get_settings()
    Use get_settings.cache_clear() in tests to reload environment variables.
    """
    return Settings()
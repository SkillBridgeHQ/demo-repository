import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport
from unittest.mock import AsyncMock, patch, MagicMock

from app.main import app
from app.db.session import get_db
from app.core.rate_limit import RedisRateLimiter, RateLimitDecision, RateLimitBackendError
from app.schemas.waitlist import WaitlistVerifyResponse, WaitlistRegisterResponse, WaitlistResendVerificationResponse
from app.services.waitlist_service import (
    ConsentVersionMismatchError,
    ConsentConfigurationError,
    TransactionBoundaryError,
    WaitlistServiceError,
    InvalidVerificationTokenError,
    VipCapacityInvariantError
)

VALID_TOKEN = "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0u1v"  # 43 URL-safe characters

# ==========================================
# Fixtures for Safe Isolation & Integration
# ==========================================

@pytest.fixture
def mock_db_tracker():
    """Spy used to mathematically prove whether get_db was invoked."""
    return MagicMock()

@pytest.fixture
def mock_db_session(mock_db_tracker):
    def _get_db_override():
        mock_db_tracker() # Mark execution
        yield MagicMock()
    return _get_db_override

@pytest.fixture
def mock_waitlist_service():
    """Mocks the strictly verified service layer logic."""
    with patch("app.api.routes.waitlist.waitlist_service") as svc:
        svc.register_waitlist_entry.return_value = WaitlistRegisterResponse()
        svc.verify_waitlist_email.return_value = WaitlistVerifyResponse(
            verified=True,
            reference_code="testrefcode123",
            vip_number=42,
            message="Your email is verified and your VIP slot is secured."
        )
        svc.resend_verification_email.return_value = WaitlistResendVerificationResponse()
        yield svc

@pytest.fixture
def mock_rate_limiter():
    """Valid strict Mock fulfilling isinstance(RedisRateLimiter)."""
    mock_redis = AsyncMock()
    identity_key = b"x" * 32
    limiter = RedisRateLimiter(redis_client=mock_redis, identity_key=identity_key)
    limiter.check = AsyncMock(return_value=RateLimitDecision(
        allowed=True, limit=5, remaining=4, retry_after_seconds=0
    ))
    return limiter

@pytest_asyncio.fixture
async def async_client(mock_db_session, mock_rate_limiter):
    """
    HTTPX Test Client wrapped with clean setup and teardown for
    dependency overrides and app state, guaranteeing isolation.
    """
    old_overrides = app.dependency_overrides.copy()
    old_limiter = getattr(app.state, "rate_limiter", None)

    app.dependency_overrides[get_db] = mock_db_session
    app.state.rate_limiter = mock_rate_limiter

    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, mock_rate_limiter
    finally:
        app.dependency_overrides = old_overrides
        app.state.rate_limiter = old_limiter


# ==========================================
# 10 ORIGINAL TESTS PRESERVED
# ==========================================

@pytest.mark.asyncio
async def test_health_endpoint(async_client):
    """Original 1: Verifies health probe returns 200."""
    client, _ = async_client
    resp = await client.get("/health")
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_openapi_contains_required_endpoints(async_client):
    """Original 2: Discovers exactly the required endpoints and methods."""
    client, _ = async_client
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200, "OpenAPI JSON should return HTTP 200 in testing environments."

    paths = resp.json().get("paths", {})

    assert "/health" in paths
    assert "get" in paths["/health"]

    assert "/api/v1/waitlist/register" in paths
    assert "post" in paths["/api/v1/waitlist/register"]

    assert "/api/v1/waitlist/verify" in paths
    assert "post" in paths["/api/v1/waitlist/verify"]

@pytest.mark.asyncio
async def test_registration_http_202(async_client, mock_waitlist_service):
    """Original 3: Successful registration."""
    client, _ = async_client
    resp = await client.post("/api/v1/waitlist/register", json={"email": "test@test.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 202

@pytest.mark.asyncio
async def test_registration_stale_consent_http_409(async_client, mock_waitlist_service):
    """Original 4: Maps ConsentVersionMismatchError to specific HTTP 409 message."""
    client, _ = async_client
    mock_waitlist_service.register_waitlist_entry.side_effect = ConsentVersionMismatchError
    resp = await client.post("/api/v1/waitlist/register", json={"email": "test@test.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The consent notice has changed. Reload the page and accept the current consent notice."

@pytest.mark.asyncio
async def test_registration_consent_config_http_503(async_client, mock_waitlist_service):
    """Original 5: Maps ConsentConfigurationError to specific HTTP 503 message."""
    client, _ = async_client
    mock_waitlist_service.register_waitlist_entry.side_effect = ConsentConfigurationError
    resp = await client.post("/api/v1/waitlist/register", json={"email": "test@test.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Registration is temporarily unavailable."

@pytest.mark.asyncio
async def test_invalid_registration_sanitization(async_client):
    """Original 6: Invalid payloads are intercepted securely by FastAPI validation."""
    client, _ = async_client
    resp = await client.post("/api/v1/waitlist/register", json={"email": "not-an-email"})
    assert resp.status_code == 422
    assert "not-an-email" not in str(resp.json())

@pytest.mark.asyncio
async def test_successful_verification_http_200(async_client, mock_waitlist_service):
    """Original 7: Successful token verification using a valid 43-character token."""
    client, _ = async_client
    resp = await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert resp.status_code == 200

@pytest.mark.asyncio
async def test_invalid_token_http_400(async_client, mock_waitlist_service):
    """Original 8: Maps InvalidVerificationTokenError to specific HTTP 400 message."""
    client, _ = async_client
    mock_waitlist_service.verify_waitlist_email.side_effect = InvalidVerificationTokenError
    resp = await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "The verification link is invalid, expired, or has already been used."

@pytest.mark.asyncio
async def test_invalid_verification_payload_sanitization(async_client):
    """Original 9: Invalid tokens are intercepted by validation and payload is sanitized."""
    client, _ = async_client
    # Failure 1 Fix: Use a highly unique, sensitive string to prevent substring false-positives
    sensitive_token = "SECRET_SENSITIVE_INVALID_TOKEN"
    resp = await client.post("/api/v1/waitlist/verify", json={"token": sensitive_token})

    assert resp.status_code == 422
    data = resp.json()

    # Assert exact submitted value is completely absent
    assert sensitive_token not in str(data)

    # Verify strict error formatting (no 'input' or 'ctx' leaks)
    assert "detail" in data
    assert isinstance(data["detail"], list)
    for error_item in data["detail"]:
        assert "input" not in error_item
        assert "ctx" not in error_item
        assert set(error_item.keys()) == {"loc", "msg", "type"}

@pytest.mark.asyncio
async def test_missing_vip_capacity_http_503(async_client, mock_waitlist_service):
    """Original 10: Maps VipCapacityInvariantError to specific HTTP 503 message."""
    client, _ = async_client
    mock_waitlist_service.verify_waitlist_email.side_effect = VipCapacityInvariantError
    resp = await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Email verification is temporarily unavailable."


# ==========================================
# REGISTRATION RATE LIMIT TESTS (11-20)
# ==========================================

@pytest.mark.asyncio
async def test_reg_allowed_reaches_service(async_client, mock_waitlist_service):
    """Reg 1: Allowed request reaches existing registration service."""
    client, limiter = async_client
    await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    mock_waitlist_service.register_waitlist_entry.assert_called_once()

@pytest.mark.asyncio
async def test_reg_rate_limited_returns_429(async_client):
    """Reg 2: HTTP 429 when limit exceeded."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=30)
    resp = await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 429

@pytest.mark.asyncio
async def test_reg_service_not_called_after_rejection(async_client, mock_waitlist_service):
    """Reg 3: Registration service not called after rejection."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=30)
    await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    mock_waitlist_service.register_waitlist_entry.assert_not_called()

@pytest.mark.asyncio
async def test_reg_get_db_not_called_after_rejection(async_client, mock_db_tracker):
    """Reg 4: get_db not called after rejection (Shielding proven)."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=30)
    await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    mock_db_tracker.assert_not_called()

@pytest.mark.asyncio
async def test_reg_redis_backend_fails_503(async_client):
    """Reg 5: HTTP 503 if Redis backend fails."""
    client, limiter = async_client
    limiter.check.side_effect = RateLimitBackendError
    resp = await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 503

@pytest.mark.asyncio
async def test_reg_missing_limiter_503(async_client):
    """Reg 6: HTTP 503 if limiter is missing."""
    client, _ = async_client
    app.state.rate_limiter = None
    resp = await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 503

@pytest.mark.asyncio
async def test_reg_headers_appear_on_allowed(async_client, mock_waitlist_service):
    """Reg 7: Rate-limit headers appear on allowed response (Failure 2 Fix)."""
    client, limiter = async_client
    resp = await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.status_code == 202
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Remaining" in resp.headers
    mock_waitlist_service.register_waitlist_entry.assert_called_once()

@pytest.mark.asyncio
async def test_reg_retry_after_on_rejected(async_client):
    """Reg 8: Retry-After appears on rejected response."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=42)
    resp = await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert resp.headers["Retry-After"] == "42"

@pytest.mark.asyncio
async def test_reg_scope_is_correct(async_client, mock_waitlist_service):
    """Reg 9: Registration scope is correct."""
    client, limiter = async_client
    await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert limiter.check.call_args.kwargs["scope"] == "registration"

@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_reg_limit_is_used(mock_get_settings, async_client, mock_waitlist_service):
    """Reg 10: REGISTRATION_RATE_LIMIT is used."""
    client, limiter = async_client

    mock_settings = MagicMock()
    mock_settings.REGISTRATION_RATE_LIMIT = 77
    mock_settings.TRUSTED_PROXY_CIDRS = ["127.0.0.1/32"]
    mock_get_settings.return_value = mock_settings

    await client.post("/api/v1/waitlist/register", json={"email": "t@t.com", "consent_accepted": True, "consent_version": "v1.0"})
    assert limiter.check.call_args.kwargs["limit"] == 77


# ==========================================
# VERIFICATION RATE LIMIT TESTS (21-28)
# ==========================================

@pytest.mark.asyncio
async def test_ver_allowed_reaches_service(async_client, mock_waitlist_service):
    """Ver 11: Allowed request reaches existing verification service."""
    client, limiter = async_client
    await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    mock_waitlist_service.verify_waitlist_email.assert_called_once()

@pytest.mark.asyncio
async def test_ver_rate_limited_returns_429(async_client):
    """Ver 12: HTTP 429 when limit exceeded."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=30)
    resp = await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert resp.status_code == 429

@pytest.mark.asyncio
async def test_ver_service_not_called_after_rejection(async_client, mock_waitlist_service):
    """Ver 13: Verification service not called after rejection."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=30)
    await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    mock_waitlist_service.verify_waitlist_email.assert_not_called()

@pytest.mark.asyncio
async def test_ver_get_db_not_called_after_rejection(async_client, mock_db_tracker):
    """Ver 14: get_db not called after rejection."""
    client, limiter = async_client
    limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=30)
    await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    mock_db_tracker.assert_not_called()

@pytest.mark.asyncio
async def test_ver_redis_backend_fails_503(async_client):
    """Ver 15: HTTP 503 on Redis backend failure."""
    client, limiter = async_client
    limiter.check.side_effect = RateLimitBackendError
    resp = await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert resp.status_code == 503

@pytest.mark.asyncio
async def test_ver_scope_is_correct(async_client, mock_waitlist_service):
    """Ver 16: Verification scope is correct."""
    client, limiter = async_client
    await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert limiter.check.call_args.kwargs["scope"] == "verification"

@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_ver_limit_is_used(mock_get_settings, async_client, mock_waitlist_service):
    """Ver 17: VERIFICATION_RATE_LIMIT is used."""
    client, limiter = async_client

    mock_settings = MagicMock()
    mock_settings.VERIFICATION_RATE_LIMIT = 88
    mock_settings.TRUSTED_PROXY_CIDRS = ["127.0.0.1/32"]
    mock_get_settings.return_value = mock_settings

    await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert limiter.check.call_args.kwargs["limit"] == 88

@pytest.mark.asyncio
async def test_ver_headers_appear_on_allowed(async_client, mock_waitlist_service):
    """Ver 18: Rate-limit headers appear on successful response (Failure 3 Fix)."""
    client, limiter = async_client
    resp = await client.post("/api/v1/waitlist/verify", json={"token": VALID_TOKEN})
    assert resp.status_code == 200
    assert "X-RateLimit-Limit" in resp.headers
    assert "X-RateLimit-Remaining" in resp.headers
    mock_waitlist_service.verify_waitlist_email.assert_called_once()

# ==========================================
# RESEND VERIFICATION RATE LIMIT TESTS
# ==========================================

@pytest.mark.asyncio
async def test_resend_allowed_reaches_service(async_client, mock_waitlist_service):
    client, limiter = async_client

    response = await client.post(
        "/api/v1/waitlist/resend-verification",
        json={"email": "Aryan@example.com"},
    )

    assert response.status_code == 202
    assert response.json() == WaitlistResendVerificationResponse().model_dump()
    mock_waitlist_service.resend_verification_email.assert_called_once()

    call = mock_waitlist_service.resend_verification_email.call_args
    assert call.kwargs["email"] == "Aryan@example.com"

    assert limiter.check.call_count == 2
    assert limiter.check.call_args_list[0].kwargs["scope"] == "resend_verification"
    assert limiter.check.call_args_list[1].kwargs["scope"] == "resend_email"


@pytest.mark.asyncio
async def test_resend_ip_rate_limited_before_database(async_client, mock_db_tracker):
    client, limiter = async_client

    limiter.check.return_value = RateLimitDecision(
        allowed=False,
        limit=3,
        remaining=0,
        retry_after_seconds=30,
    )

    response = await client.post(
        "/api/v1/waitlist/resend-verification",
        json={"email": "test@example.com"},
    )

    assert response.status_code == 429
    limiter.check.assert_called_once()
    mock_db_tracker.assert_not_called()


@pytest.mark.asyncio
async def test_resend_email_cooldown_rejected_before_database(async_client, mock_db_tracker):
    client, limiter = async_client

    limiter.check.side_effect = [
        RateLimitDecision(
            allowed=True,
            limit=3,
            remaining=2,
            retry_after_seconds=0,
        ),
        RateLimitDecision(
            allowed=False,
            limit=1,
            remaining=0,
            retry_after_seconds=240,
        ),
    ]

    response = await client.post(
        "/api/v1/waitlist/resend-verification",
        json={"email": "test@example.com"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "240"
    assert limiter.check.call_count == 2
    mock_db_tracker.assert_not_called()


@pytest.mark.asyncio
async def test_resend_redis_failure_fails_closed_before_database(async_client, mock_db_tracker):
    client, limiter = async_client

    limiter.check.side_effect = [
        RateLimitDecision(
            allowed=True,
            limit=3,
            remaining=2,
            retry_after_seconds=0,
        ),
        RateLimitBackendError(),
    ]

    response = await client.post(
        "/api/v1/waitlist/resend-verification",
        json={"email": "test@example.com"},
    )

    assert response.status_code == 503
    mock_db_tracker.assert_not_called()


@pytest.mark.asyncio
async def test_resend_uses_normalized_email_for_cooldown(async_client, mock_waitlist_service):
    client, limiter = async_client

    await client.post(
        "/api/v1/waitlist/resend-verification",
        json={"email": "  Aryan@Example.com  "},
    )

    email_check = limiter.check.call_args_list[1]
    assert email_check.kwargs["scope"] == "resend_email"
    assert email_check.kwargs["identity"] == "aryan@example.com"
    assert email_check.kwargs["limit"] == 1
    assert email_check.kwargs["window_seconds"] == 300


@pytest.mark.asyncio
async def test_resend_invalid_email_is_rejected(
    async_client,
    mock_waitlist_service,
):
    client, limiter = async_client

    response = await client.post(
        "/api/v1/waitlist/resend-verification",
        json={"email": "not-an-email"},
    )

    assert response.status_code == 422
    limiter.check.assert_not_called()
    mock_waitlist_service.resend_verification_email.assert_not_called()


@pytest.mark.asyncio
async def test_resend_endpoint_is_in_openapi(async_client):
    client, _ = async_client

    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()

    assert "/api/v1/waitlist/resend-verification" in schema["paths"]
    assert "post" in schema["paths"]["/api/v1/waitlist/resend-verification"]

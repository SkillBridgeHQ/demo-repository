import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request, Response, HTTPException

from app.core.rate_limit_dependencies import (
    get_rate_limiter,
    registration_rate_limit,
    verification_rate_limit
)
from app.core.rate_limit import (
    RedisRateLimiter,
    RateLimitDecision,
    RateLimitBackendError,
    RateLimitConfigurationError
)
from app.core.client_ip import ClientAddressError


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.TRUSTED_PROXY_CIDRS = ["127.0.0.1/32"]
    settings.REGISTRATION_RATE_LIMIT = 5
    settings.VERIFICATION_RATE_LIMIT = 10
    return settings


@pytest.fixture
def mock_limiter():
    limiter = AsyncMock(spec=RedisRateLimiter)
    limiter.check.return_value = RateLimitDecision(
        allowed=True,
        limit=5,
        remaining=4,
        retry_after_seconds=0
    )
    return limiter


@pytest.fixture
def mock_request(mock_limiter):
    req = MagicMock(spec=Request)
    req.app.state.rate_limiter = mock_limiter
    return req


@pytest.fixture
def mock_response():
    resp = MagicMock(spec=Response)
    resp.headers = {}
    return resp


@pytest.mark.asyncio
async def test_get_rate_limiter_missing(mock_request):
    """Test 1: Missing limiter returns 503."""
    mock_request.app.state.rate_limiter = None
    with pytest.raises(HTTPException) as exc:
        await get_rate_limiter(mock_request)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_get_rate_limiter_invalid_type(mock_request):
    """Test 2: Invalid limiter object returns 503."""
    mock_request.app.state.rate_limiter = "Not A Limiter"
    with pytest.raises(HTTPException) as exc:
        await get_rate_limiter(mock_request)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_get_rate_limiter_valid(mock_request):
    """Test 3: Valid limiter is retrieved."""
    limiter = await get_rate_limiter(mock_request)
    assert isinstance(limiter, RedisRateLimiter)


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_registration_allowed_and_headers(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 4, 6, 8, 9, 10, 13: Registration allowed, correct scope, limit, client IP, window, and quota headers."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.return_value = "192.168.1.100"

    await registration_rate_limit(mock_request, mock_response, mock_limiter)

    mock_get_client_ip.assert_called_once_with(mock_request, trusted_proxy_cidrs=mock_settings.TRUSTED_PROXY_CIDRS)
    mock_limiter.check.assert_called_once_with(
        scope="registration",
        identity="192.168.1.100",
        limit=5,
        window_seconds=60
    )

    assert mock_response.headers["X-RateLimit-Limit"] == "5"
    assert mock_response.headers["X-RateLimit-Remaining"] == "4"


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_verification_allowed_and_scopes(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 5, 7, 19: Verification allowed, correct scope (verifying separate scope usage)."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.return_value = "10.0.0.5"
    mock_limiter.check.return_value = RateLimitDecision(allowed=True, limit=10, remaining=9, retry_after_seconds=0)

    await verification_rate_limit(mock_request, mock_response, mock_limiter)

    mock_limiter.check.assert_called_once_with(
        scope="verification",
        identity="10.0.0.5",
        limit=10,
        window_seconds=60
    )


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_rejection_returns_429_and_headers(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 11, 12, 18: Rate-limit rejection returns 429, Retry-After header, no IP exposed."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.return_value = "172.16.0.1"

    mock_limiter.check.return_value = RateLimitDecision(allowed=False, limit=5, remaining=0, retry_after_seconds=25)

    with pytest.raises(HTTPException) as exc:
        await registration_rate_limit(mock_request, mock_response, mock_limiter)

    assert exc.value.status_code == 429
    assert "Too many requests" in exc.value.detail
    assert "172.16.0.1" not in exc.value.detail  # Verify IP is not exposed

    assert exc.value.headers["X-RateLimit-Limit"] == "5"
    assert exc.value.headers["X-RateLimit-Remaining"] == "0"
    assert exc.value.headers["Retry-After"] == "25"


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_backend_failure_returns_503(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 14: Redis backend failure returns 503."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.return_value = "192.168.1.1"

    mock_limiter.check.side_effect = RateLimitBackendError("Connection timeout")

    with pytest.raises(HTTPException) as exc:
        await registration_rate_limit(mock_request, mock_response, mock_limiter)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_configuration_error_returns_503(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 15: RateLimitConfigurationError returns 503 fail-closed."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.return_value = "192.168.1.1"

    mock_limiter.check.side_effect = RateLimitConfigurationError("Bad Redis URI")

    with pytest.raises(HTTPException) as exc:
        await registration_rate_limit(mock_request, mock_response, mock_limiter)

    assert exc.value.status_code == 503


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_invalid_client_address_returns_400(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 16: Invalid client address returns 400."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.side_effect = ClientAddressError("Malformed X-Forwarded-For")

    with pytest.raises(HTTPException) as exc:
        await verification_rate_limit(mock_request, mock_response, mock_limiter)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid client network routing."


@pytest.mark.asyncio
@patch("app.core.rate_limit_dependencies.get_client_ip")
@patch("app.core.rate_limit_dependencies.get_settings")
async def test_unexpected_exceptions_propagate(mock_get_settings, mock_get_client_ip, mock_request, mock_response, mock_settings, mock_limiter):
    """Test 17: Unexpected exceptions do not silently allow requests."""
    mock_get_settings.return_value = mock_settings
    mock_get_client_ip.return_value = "10.10.10.10"

    mock_limiter.check.side_effect = ValueError("Unexpected application crash")

    with pytest.raises(ValueError, match="Unexpected application crash"):
        await registration_rate_limit(mock_request, mock_response, mock_limiter)

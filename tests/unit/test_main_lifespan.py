import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi import Request
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError

from app.main import app, validation_exception_handler
from app.core.rate_limit import RedisRateLimiter

@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.APP_NAME = "Test App"
    settings.DEBUG = True
    settings.APP_ENV = "testing"
    settings.REDIS_URL = None
    settings.ENCRYPTION_KEY_V1 = MagicMock()
    settings.ENCRYPTION_KEY_V1.get_secret_value.return_value = "mocked-secret-key"

    settings.ENCRYPTION_ACTIVE_KEY_VERSION = "v2"
    settings.ENCRYPTION_KEY_V2 = MagicMock()
    settings.ENCRYPTION_KEY_V2.get_secret_value.return_value = "mocked-v2-secret-key"
    settings.CORS_ORIGINS = ["http://localhost:5173"]
    return settings

@pytest.mark.asyncio
@patch("app.main.get_settings")
async def test_no_redis_url_startup(mock_get_settings, mock_settings):
    """Test: Missing Redis URL startup behavior."""
    mock_get_settings.return_value = mock_settings
    mock_settings.REDIS_URL = None

    async with app.router.lifespan_context(app):
        assert app.state.rate_limiter is None

@pytest.mark.asyncio
@patch("app.main.RedisRateLimiter")
@patch("app.main.derive_rate_limit_identity_key")
@patch("app.main.redis.from_url")
@patch("app.main.get_settings")
async def test_redis_startup_success_and_shutdown(mock_get_settings, mock_from_url, mock_derive, mock_limiter_class, mock_settings):
    """Test: Full successful lifecycle."""
    mock_get_settings.return_value = mock_settings

    mock_redis_url = MagicMock()
    mock_redis_url.get_secret_value.return_value = "rediss://fake-redis"
    mock_settings.REDIS_URL = mock_redis_url

    mock_redis_client = AsyncMock()
    mock_from_url.return_value = mock_redis_client

    valid_32_byte_key = b"x" * 32
    mock_derive.return_value = valid_32_byte_key

    mock_limiter_instance = MagicMock(spec=RedisRateLimiter)
    mock_limiter_class.return_value = mock_limiter_instance

    async with app.router.lifespan_context(app):
        # Startup
        mock_from_url.assert_called_once_with("rediss://fake-redis", socket_timeout=3.0, socket_connect_timeout=3.0, decode_responses=True)
        mock_redis_client.ping.assert_awaited_once()
        mock_derive.assert_called_once_with("mocked-v2-secret-key")

        mock_limiter_class.assert_called_once_with(
            redis_client=mock_redis_client,
            identity_key=valid_32_byte_key
        )
        assert app.state.rate_limiter is mock_limiter_instance

    # Shutdown
    assert app.state.rate_limiter is None
    mock_redis_client.aclose.assert_awaited_once()

@pytest.mark.asyncio
@patch("app.main.redis.from_url")
@patch("app.main.get_settings")
async def test_startup_ping_failure(mock_get_settings, mock_from_url, mock_settings):
    """Test: Ping failure aborts correctly without leaking secrets."""
    mock_get_settings.return_value = mock_settings
    mock_redis_url = MagicMock()
    mock_redis_url.get_secret_value.return_value = "rediss://secret:password@fake-redis"
    mock_settings.REDIS_URL = mock_redis_url

    mock_redis_client = AsyncMock()
    mock_redis_client.ping.side_effect = Exception("Connection Timeout")
    mock_from_url.return_value = mock_redis_client

    with pytest.raises(RuntimeError) as exc:
        async with app.router.lifespan_context(app):
            pass

    assert str(exc.value) == "Failed to initialize Redis rate limiting service."
    assert "secret:password" not in str(exc.value)
    mock_redis_client.aclose.assert_awaited_once()
    assert app.state.rate_limiter is None

@pytest.mark.asyncio
@patch("app.main.RedisRateLimiter")
@patch("app.main.derive_rate_limit_identity_key")
@patch("app.main.redis.from_url")
@patch("app.main.get_settings")
async def test_startup_limiter_constructor_failure(mock_get_settings, mock_from_url, mock_derive, mock_limiter_class, mock_settings):
    """Test: Redis client created and pinged, but Limiter construction raises exception."""
    mock_get_settings.return_value = mock_settings
    mock_redis_url = MagicMock()
    mock_redis_url.get_secret_value.return_value = "rediss://fake-redis"
    mock_settings.REDIS_URL = mock_redis_url

    mock_redis_client = AsyncMock()
    mock_from_url.return_value = mock_redis_client
    mock_derive.return_value = b"y" * 32

    # Simulate a crash during construction
    mock_limiter_class.side_effect = ValueError("Invalid identity key size")

    with pytest.raises(RuntimeError) as exc:
        async with app.router.lifespan_context(app):
            pass

    # Aborts with identical generic error
    assert str(exc.value) == "Failed to initialize Redis rate limiting service."
    # Client cleanup is attempted exactly once (the `finally` block won't execute if error occurs before `yield`)
    mock_redis_client.aclose.assert_awaited_once()
    assert app.state.rate_limiter is None

@pytest.mark.asyncio
@patch("app.main.redis.from_url")
@patch("app.main.get_settings")
async def test_startup_cancellation(mock_get_settings, mock_from_url, mock_settings):
    """Test: Cancellation does not get swallowed, but cleanup still runs."""
    mock_get_settings.return_value = mock_settings
    mock_settings.REDIS_URL = MagicMock()

    mock_redis_client = AsyncMock()
    mock_redis_client.ping.side_effect = asyncio.CancelledError()
    mock_from_url.return_value = mock_redis_client

    with pytest.raises(asyncio.CancelledError):
        async with app.router.lifespan_context(app):
            pass

    mock_redis_client.aclose.assert_awaited_once()
    assert app.state.rate_limiter is None

def test_cors_preserves_original_config_and_adds_headers():
    """Test: CORS preserves tight origin/methods and adds RateLimit headers."""
    from fastapi.middleware.cors import CORSMiddleware

    cors_mw = None
    for mw in app.user_middleware:
        if mw.cls == CORSMiddleware:
            cors_mw = mw
            break

    assert cors_mw is not None
    assert cors_mw.kwargs.get("allow_credentials") is False
    assert "OPTIONS" in cors_mw.kwargs.get("allow_methods", [])
    assert "GET" in cors_mw.kwargs.get("allow_methods", [])
    assert "Content-Type" in cors_mw.kwargs.get("allow_headers", [])
    assert cors_mw.kwargs.get("max_age") == 600

    exposed = cors_mw.kwargs.get("expose_headers", [])
    assert "X-RateLimit-Limit" in exposed
    assert "X-RateLimit-Remaining" in exposed
    assert "Retry-After" in exposed

def test_router_and_health_integrity():
    """Test: Routing integrity and deduplication using OpenAPI schema."""
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200

    # Safely inspect the API contract via OpenAPI paths instead of internal router objects
    paths = app.openapi()["paths"]

    assert "/health" in paths
    assert "/api/v1/waitlist/register" in paths
    assert "/api/v1/waitlist/verify" in paths

    assert "get" in paths["/health"]
    assert "post" in paths["/api/v1/waitlist/register"]
    assert "post" in paths["/api/v1/waitlist/verify"]

    # Ensure accidental double routing prefix like `/api/v1/waitlist/api/v1...` doesn't exist
    for path_key in paths.keys():
        assert path_key.count("/api/v1/waitlist") <= 1

@pytest.mark.asyncio
async def test_validation_handler_sanitizes_input():
    """Regression Test: Ensures Pydantic exceptions are stripped of sensitive payload context."""
    mock_req = MagicMock(spec=Request)
    mock_exc = MagicMock(spec=RequestValidationError)

    # Simulate a validation error containing highly sensitive original input data
    mock_exc.errors.return_value = [
        {
            "loc": ("body", "token"),
            "msg": "String should have at least 32 characters",
            "type": "string_too_short",
            "input": "super_secret_raw_token_value",
            "ctx": {"min_length": 32}
        }
    ]

    response = await validation_exception_handler(mock_req, mock_exc)

    assert response.status_code == 422
    import json
    data = json.loads(response.body)

    assert isinstance(data["detail"], list)
    assert len(data["detail"]) == 1

    item = data["detail"][0]
    # Keys strictly limited to exactly loc, msg, type
    assert list(item.keys()) == ["loc", "msg", "type"]

    assert item["loc"] == ["body", "token"]
    assert item["msg"] == "String should have at least 32 characters"
    assert item["type"] == "string_too_short"

    # Verify strict stripping of context and raw inputs
    assert "input" not in item
    assert "ctx" not in item
    assert "super_secret_raw_token_value" not in str(data)

@pytest.mark.asyncio
@patch("app.main.get_settings")
async def test_startup_db_integrity(mock_get_settings, mock_settings):
    """Test: Application startup does not attempt database connections."""
    mock_get_settings.return_value = mock_settings
    mock_settings.REDIS_URL = None

    with patch("sqlalchemy.engine.base.Engine.connect") as mock_connect:
        async with app.router.lifespan_context(app):
            pass
        mock_connect.assert_not_called()

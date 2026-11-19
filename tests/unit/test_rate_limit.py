import base64
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.core.rate_limit import (
    RATE_LIMIT_LUA,
    RateLimitBackendError,
    RateLimitConfigurationError,
    RedisRateLimiter,
    derive_rate_limit_identity_key,
)


SOURCE_KEY = b"0123456789abcdef0123456789abcdef"
SOURCE_KEY_B64 = base64.b64encode(SOURCE_KEY).decode("ascii")
IDENTITY_KEY = derive_rate_limit_identity_key(SOURCE_KEY_B64)


def make_limiter():
    redis_client = AsyncMock()

    limiter = RedisRateLimiter(
        redis_client=redis_client,
        identity_key=IDENTITY_KEY,
    )

    return limiter, redis_client


# ---------------------------------------------------------------------
# Identity key derivation
# ---------------------------------------------------------------------

def test_identity_key_is_deterministic():
    first = derive_rate_limit_identity_key(SOURCE_KEY_B64)
    second = derive_rate_limit_identity_key(SOURCE_KEY_B64)

    assert first == second
    assert len(first) == 32


def test_identity_key_is_domain_separated():
    derived = derive_rate_limit_identity_key(SOURCE_KEY_B64)

    assert derived != SOURCE_KEY


@pytest.mark.parametrize(
    "invalid_key",
    [
        "not-valid-base64",
        base64.b64encode(b"short-key").decode("ascii"),
    ],
)
def test_identity_key_rejects_invalid_source(invalid_key):
    with pytest.raises(RateLimitConfigurationError):
        derive_rate_limit_identity_key(invalid_key)


def test_identity_key_rejects_non_string():
    with pytest.raises(RateLimitConfigurationError):
        derive_rate_limit_identity_key(None)


def test_constructor_rejects_invalid_identity_key():
    with pytest.raises(RateLimitConfigurationError):
        RedisRateLimiter(
            redis_client=AsyncMock(),
            identity_key=b"short",
        )


# ---------------------------------------------------------------------
# Redis key privacy and validation
# ---------------------------------------------------------------------

def test_redis_key_does_not_expose_client_identity():
    limiter, _ = make_limiter()

    client_ip = "203.0.113.10"

    redis_key = limiter._build_redis_key(
        "registration",
        client_ip,
    )

    assert client_ip not in redis_key
    assert redis_key.startswith(
        "skillbridge:rate-limit:v1:registration:"
    )


def test_redis_key_is_deterministic():
    limiter, _ = make_limiter()

    first = limiter._build_redis_key(
        "registration",
        "203.0.113.10",
    )

    second = limiter._build_redis_key(
        "registration",
        "203.0.113.10",
    )

    assert first == second


def test_different_scopes_use_different_keys():
    limiter, _ = make_limiter()

    registration_key = limiter._build_redis_key(
        "registration",
        "203.0.113.10",
    )

    verification_key = limiter._build_redis_key(
        "verification",
        "203.0.113.10",
    )

    assert registration_key != verification_key


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "Registration",
        "registration scope",
        "a" * 49,
    ],
)
def test_invalid_scope_is_rejected(scope):
    limiter, _ = make_limiter()

    with pytest.raises(RateLimitConfigurationError):
        limiter._build_redis_key(
            scope,
            "203.0.113.10",
        )


def test_empty_identity_is_rejected():
    limiter, _ = make_limiter()

    with pytest.raises(RateLimitConfigurationError):
        limiter._build_redis_key(
            "registration",
            "   ",
        )


# ---------------------------------------------------------------------
# Allowed and denied decisions
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_allowed_request_returns_remaining_quota():
    limiter, redis_client = make_limiter()

    redis_client.eval.return_value = [1, 2, 0]

    decision = await limiter.check(
        scope="registration",
        identity="203.0.113.10",
        limit=5,
        window_seconds=60,
    )

    assert decision.allowed is True
    assert decision.limit == 5
    assert decision.remaining == 3
    assert decision.retry_after_seconds == 0

    redis_client.eval.assert_awaited_once()

    args = redis_client.eval.await_args.args

    assert args[0] == RATE_LIMIT_LUA
    assert args[1] == 1
    assert args[2].startswith(
        "skillbridge:rate-limit:v1:registration:"
    )
    assert args[3] == 60000
    assert args[4] == 5
    assert len(args[5]) == 32


@pytest.mark.asyncio
async def test_denied_request_returns_retry_after():
    limiter, redis_client = make_limiter()

    redis_client.eval.return_value = [0, 5, 1501]

    decision = await limiter.check(
        scope="registration",
        identity="203.0.113.10",
        limit=5,
    )

    assert decision.allowed is False
    assert decision.limit == 5
    assert decision.remaining == 0
    assert decision.retry_after_seconds == 2


@pytest.mark.asyncio
async def test_same_identity_and_scope_use_same_redis_key():
    limiter, redis_client = make_limiter()

    redis_client.eval.return_value = [1, 1, 0]

    await limiter.check(
        scope="verification",
        identity="203.0.113.10",
        limit=10,
    )

    first_key = redis_client.eval.await_args.args[2]

    redis_client.eval.return_value = [1, 2, 0]

    await limiter.check(
        scope="verification",
        identity="203.0.113.10",
        limit=10,
    )

    second_key = redis_client.eval.await_args.args[2]

    assert first_key == second_key


# ---------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,window",
    [
        (0, 60),
        (-1, 60),
        (True, 60),
        (5, 0),
        (5, -1),
        (5, 86401),
        (5, True),
    ],
)
async def test_invalid_rate_limit_configuration(limit, window):
    limiter, redis_client = make_limiter()

    with pytest.raises(RateLimitConfigurationError):
        await limiter.check(
            scope="registration",
            identity="203.0.113.10",
            limit=limit,
            window_seconds=window,
        )

    redis_client.eval.assert_not_awaited()


# ---------------------------------------------------------------------
# Redis failure and malformed response handling
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failure_fails_closed():
    limiter, redis_client = make_limiter()

    redis_client.eval.side_effect = RedisError(
        "Simulated Redis failure"
    )

    with pytest.raises(RateLimitBackendError):
        await limiter.check(
            scope="registration",
            identity="203.0.113.10",
            limit=5,
        )


@pytest.mark.asyncio
async def test_malformed_redis_response_is_rejected():
    limiter, redis_client = make_limiter()

    redis_client.eval.return_value = [1, 2]

    with pytest.raises(RateLimitBackendError):
        await limiter.check(
            scope="registration",
            identity="203.0.113.10",
            limit=5,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redis_result",
    [
        ["invalid", 2, 0],
        [2, 2, 0],
        [1, -1, 0],
        [1, 6, 0],
        [0, 5, -1],
    ],
)
async def test_invalid_redis_result_values_are_rejected(redis_result):
    limiter, redis_client = make_limiter()

    redis_client.eval.return_value = redis_result

    with pytest.raises(RateLimitBackendError):
        await limiter.check(
            scope="registration",
            identity="203.0.113.10",
            limit=5,
        )


# ---------------------------------------------------------------------
# Lua script invariants
# ---------------------------------------------------------------------

def test_lua_script_uses_redis_server_time():
    assert "redis.call('TIME')" in RATE_LIMIT_LUA


def test_lua_script_uses_atomic_sliding_window_operations():
    assert "ZREMRANGEBYSCORE" in RATE_LIMIT_LUA
    assert "ZCARD" in RATE_LIMIT_LUA
    assert "ZADD" in RATE_LIMIT_LUA
    assert "PEXPIRE" in RATE_LIMIT_LUA

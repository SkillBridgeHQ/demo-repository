"""
Redis-backed distributed sliding-window rate limiter.

Uses an atomic Lua script and Redis server time so that multiple
application workers share the same rate-limit state.

Client IP addresses are never stored directly in Redis keys.
"""

import base64
import binascii
import hashlib
import hmac
import math
import re
import secrets

from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError


RATE_LIMIT_LUA = """
local now_parts = redis.call('TIME')
local now_us = (tonumber(now_parts[1]) * 1000000)
               + tonumber(now_parts[2])

local window_ms = tonumber(ARGV[1])
local window_us = window_ms * 1000
local limit = tonumber(ARGV[2])
local request_id = ARGV[3]

local cutoff = now_us - window_us

redis.call(
    'ZREMRANGEBYSCORE',
    KEYS[1],
    '-inf',
    cutoff
)

local current_count = redis.call('ZCARD', KEYS[1])

if current_count < limit then
    redis.call(
        'ZADD',
        KEYS[1],
        now_us,
        request_id
    )

    redis.call(
        'PEXPIRE',
        KEYS[1],
        window_ms
    )

    return {1, current_count + 1, 0}
end

local oldest = redis.call(
    'ZRANGE',
    KEYS[1],
    0,
    0,
    'WITHSCORES'
)

local retry_ms = math.ceil(
    (tonumber(oldest[2]) + window_us - now_us) / 1000
)

if retry_ms < 1 then
    retry_ms = 1
end

return {0, current_count, retry_ms}
"""


class RateLimitConfigurationError(ValueError):
    """Raised for invalid rate-limit configuration."""


class RateLimitBackendError(RuntimeError):
    """Raised when Redis cannot safely enforce a rate limit."""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


def derive_rate_limit_identity_key(encryption_key_b64: str) -> bytes:
    """
    Derive a dedicated HMAC key from the application's AES key.

    Domain separation prevents direct reuse of the AES key for HMAC.
    """

    if not isinstance(encryption_key_b64, str):
        raise RateLimitConfigurationError(
            "Invalid rate-limit identity key configuration."
        )

    try:
        encryption_key = base64.b64decode(
            encryption_key_b64,
            validate=True,
        )
    except (binascii.Error, ValueError, UnicodeError):
        raise RateLimitConfigurationError(
            "Invalid rate-limit identity key configuration."
        ) from None

    if len(encryption_key) != 32:
        raise RateLimitConfigurationError(
            "Rate-limit identity derivation requires a 32-byte source key."
        )

    return hmac.new(
        encryption_key,
        b"skillbridge-waitlist:rate-limit-identity:v1",
        hashlib.sha256,
    ).digest()


class RedisRateLimiter:
    """Atomic distributed rate limiter backed by Redis."""

    def __init__(
        self,
        redis_client: Redis,
        identity_key: bytes,
    ) -> None:

        if not isinstance(identity_key, bytes) or len(identity_key) != 32:
            raise RateLimitConfigurationError(
                "Rate-limit identity key must contain exactly 32 bytes."
            )

        self._redis = redis_client
        self._identity_key = bytes(identity_key)

    def _build_redis_key(
        self,
        scope: str,
        identity: str,
    ) -> str:
        """Build a privacy-preserving Redis key."""

        if not isinstance(scope, str) or not re.fullmatch(
            r"[a-z][a-z0-9_-]{0,47}",
            scope,
        ):
            raise RateLimitConfigurationError(
                "Invalid rate-limit scope."
            )

        if not isinstance(identity, str) or not identity.strip():
            raise RateLimitConfigurationError(
                "Invalid rate-limit identity."
            )

        message = (
            scope.encode("utf-8")
            + b"\x00"
            + identity.encode("utf-8")
        )

        digest = hmac.new(
            self._identity_key,
            message,
            hashlib.sha256,
        ).hexdigest()

        return f"skillbridge:rate-limit:v1:{scope}:{digest}"

    async def check(
        self,
        *,
        scope: str,
        identity: str,
        limit: int,
        window_seconds: int = 60,
    ) -> RateLimitDecision:
        """
        Atomically check and consume one request from a sliding window.

        The operation is serialized by Redis Lua execution.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise RateLimitConfigurationError(
                "Rate-limit value must be a positive integer."
            )

        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 1 <= window_seconds <= 86400
        ):
            raise RateLimitConfigurationError(
                "Rate-limit window must be between 1 and 86400 seconds."
            )

        redis_key = self._build_redis_key(scope, identity)

        window_ms = window_seconds * 1000

        # Unique member prevents requests in the same microsecond
        # from overwriting each other in the Redis sorted set.
        request_id = secrets.token_hex(16)

        try:
            result = await self._redis.eval(
                RATE_LIMIT_LUA,
                1,
                redis_key,
                window_ms,
                limit,
                request_id,
            )

        except (RedisError, OSError, TimeoutError):
            raise RateLimitBackendError(
                "Rate-limit storage is temporarily unavailable."
            ) from None

        if not isinstance(result, (list, tuple)) or len(result) != 3:
            raise RateLimitBackendError(
                "Rate-limit storage returned an invalid response."
            )

        try:
            allowed_raw, count_raw, retry_raw = result

            allowed_value = int(allowed_raw)
            count = int(count_raw)
            retry_ms = int(retry_raw)

        except (TypeError, ValueError, OverflowError):
            raise RateLimitBackendError(
                "Rate-limit storage returned an invalid response."
            ) from None

        if (
            allowed_value not in (0, 1)
            or count < 0
            or retry_ms < 0
            or (allowed_value == 1 and count > limit)
        ):
            raise RateLimitBackendError(
                "Rate-limit storage returned an invalid response."
            )

        allowed = allowed_value == 1

        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after_seconds=(
                math.ceil(retry_ms / 1000)
                if not allowed
                else 0
            ),
        )

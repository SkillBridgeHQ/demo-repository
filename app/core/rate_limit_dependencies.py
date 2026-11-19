"""
Rate Limiting Dependencies

Provides FastAPI dependency injection functions to enforce distributed rate limits
using Redis. Enforces strict fail-closed behavior on backend unavailability and
securely handles client IP resolution.
"""

from fastapi import Request, Response, HTTPException, Depends

from app.core.config import get_settings
from app.core.client_ip import get_client_ip, ClientAddressError
from app.schemas.waitlist import WaitlistResendVerificationRequest

from app.core.rate_limit import (
    RedisRateLimiter,
    RateLimitBackendError,
    RateLimitConfigurationError,
    RateLimitDecision,
)


async def get_rate_limiter(request: Request) -> RedisRateLimiter:
    """
    Retrieves the RedisRateLimiter instance from the application state.

    Fail-closed: If the limiter is not initialized (e.g., Redis connection failure
    during startup), this dependency forcibly aborts the request with an HTTP 503.
    """
    limiter = getattr(request.app.state, "rate_limiter", None)
    if not limiter or not isinstance(limiter, RedisRateLimiter):
        raise HTTPException(
            status_code=503,
            detail="Rate limiting service is temporarily unavailable."
        )
    return limiter


async def _enforce_rate_limit(
    request: Request,
    response: Response,
    limiter: RedisRateLimiter,
    scope: str,
    limit: int,
    window_seconds: int = 60
) -> None:
    """
    Core rate limit enforcement logic.

    Retrieves the trusted client IP, queries the rate limiter, and attaches the
    appropriate HTTP headers. Throws 429 Too Many Requests if the limit is exceeded.
    """
    settings = get_settings()

    # 1. Identity Resolution (Fail-closed on invalid proxy state)
    try:
        client_ip = get_client_ip(
            request,
            trusted_proxy_cidrs=settings.TRUSTED_PROXY_CIDRS
        )
    except ClientAddressError:
        raise HTTPException(
            status_code=400,
            detail="Invalid client network routing."
        )

    # 2. Rate Limit Verification (Fail-closed on configuration/Redis failure)
    try:
        decision: RateLimitDecision = await limiter.check(
            scope=scope,
            identity=client_ip,
            limit=limit,
            window_seconds=window_seconds
        )
    except (RateLimitBackendError, RateLimitConfigurationError):
        raise HTTPException(
            status_code=503,
            detail="Rate limiting service is temporarily unavailable."
        )

    # 3. Handle Rejection
    if not decision.allowed:
        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "Retry-After": str(decision.retry_after_seconds)
        }
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers=headers
        )

    # 4. Handle Success
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)


async def registration_rate_limit(
    request: Request,
    response: Response,
    limiter: RedisRateLimiter = Depends(get_rate_limiter)
) -> None:
    """
    FastAPI dependency for Waitlist Registration rate limiting.
    """
    settings = get_settings()
    await _enforce_rate_limit(
        request=request,
        response=response,
        limiter=limiter,
        scope="registration",
        limit=settings.REGISTRATION_RATE_LIMIT,
        window_seconds=60
    )


async def verification_rate_limit(
    request: Request,
    response: Response,
    limiter: RedisRateLimiter = Depends(get_rate_limiter)
) -> None:
    """
    FastAPI dependency for Waitlist Verification rate limiting.
    """
    settings = get_settings()
    await _enforce_rate_limit(
        request=request,
        response=response,
        limiter=limiter,
        scope="verification",
        limit=settings.VERIFICATION_RATE_LIMIT,
        window_seconds=60
    )

async def resend_verification_rate_limit(
    payload: WaitlistResendVerificationRequest,
    request: Request,
    response: Response,
    limiter: RedisRateLimiter = Depends(get_rate_limiter),
) -> WaitlistResendVerificationRequest:
    """
    Enforce both IP-based and email-based resend limits.

    The email identity is passed to RedisRateLimiter, which HMACs
    the identity before constructing its Redis key.

    This check runs before get_db because the route receives its
    validated payload through this dependency.
    """

    settings = get_settings()

    # First: distributed IP-level limit.
    await _enforce_rate_limit(
        request=request,
        response=response,
        limiter=limiter,
        scope="resend_verification",
        limit=settings.RESEND_RATE_LIMIT,
        window_seconds=60,
    )

    # Second: per-email cooldown. Applied equally to every address.
    normalized_email = str(payload.email).strip().casefold()

    try:
        decision: RateLimitDecision = await limiter.check(
            scope="resend_email",
            identity=normalized_email,
            limit=1,
            window_seconds=settings.RESEND_EMAIL_COOLDOWN_SECONDS,
        )
    except (RateLimitBackendError, RateLimitConfigurationError):
        raise HTTPException(
            status_code=503,
            detail="Rate limiting service is temporarily unavailable.",
        ) from None

    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={
                "X-Email-RateLimit-Limit": str(decision.limit),
                "X-Email-RateLimit-Remaining": str(decision.remaining),
                "Retry-After": str(decision.retry_after_seconds),
            },
        )

    response.headers["X-Email-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-Email-RateLimit-Remaining"] = str(decision.remaining)

    return payload

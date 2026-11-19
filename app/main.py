import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import redis.asyncio as redis

from app.core.config import get_settings
from app.core.rate_limit import RedisRateLimiter, derive_rate_limit_identity_key
from app.api.routes.waitlist import router as waitlist_router

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager for global resources.
    Safely manages the Redis connection pool for distributed rate limiting.
    """
    settings = get_settings()

    app.state.rate_limiter = None
    redis_client = None

    if settings.REDIS_URL:
        try:
            redis_client = redis.from_url(
                settings.REDIS_URL.get_secret_value(),
                socket_timeout=3.0,
                socket_connect_timeout=3.0,
                decode_responses=True
            )
            await redis_client.ping()

            identity_key_bytes = derive_rate_limit_identity_key(
                settings.ENCRYPTION_KEY_V1.get_secret_value()
            )

            app.state.rate_limiter = RedisRateLimiter(
                redis_client=redis_client,
                identity_key=identity_key_bytes
            )

        except asyncio.CancelledError:
            # Propagate cancellation (e.g., SIGTERM during startup) upwards naturally
            # Ensure cleanup failures do not replace the original CancelledError
            if redis_client:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass
            raise
        except Exception:
            # Initialization failure (e.g., timeout, DNS failure, constructor exception)
            if redis_client:
                try:
                    await redis_client.aclose()
                except Exception:
                    pass
            raise RuntimeError("Failed to initialize Redis rate limiting service.") from None

    try:
        yield
    finally:
        # Graceful Shutdown
        app.state.rate_limiter = None
        if redis_client:
            try:
                await redis_client.aclose()
            except Exception:
                pass


settings = get_settings()
is_production = settings.APP_ENV == "production"

app = FastAPI(
    title=settings.APP_NAME,
    description="SkillBridge early-access waitlist API.",
    debug=settings.DEBUG,
    docs_url=None if is_production else "/docs",
    redoc_url=None if is_production else "/redoc",
    openapi_url=None if is_production else "/openapi.json",
    lifespan=lifespan,
)

# Apply CORS matching original requirements plus Rate-Limit Expose Headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=600,
    expose_headers=[
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "Retry-After"
    ]
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Sanitizes 422 responses to prevent sensitive payload reflection.
    Restricts errors strictly to loc, msg, and type.
    """
    safe_errors = []

    for error in exc.errors():
        safe_errors.append(
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", "Invalid request."),
                "type": error.get("type", "value_error"),
            }
        )

    return JSONResponse(
        status_code=422,
        content={
            "detail": safe_errors,
        },
    )

@app.get("/health")
async def health_check():
    """Lightweight liveness probe."""
    return {"status": "ok"}

# Register Waitlist Router (already prefixed as /api/v1/waitlist in the router definition)
app.include_router(waitlist_router)

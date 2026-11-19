from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.schemas.waitlist import (
    WaitlistRegisterRequest,
    WaitlistRegisterResponse,
    WaitlistResendVerificationRequest,
    WaitlistResendVerificationResponse,
    WaitlistVerifyRequest,
    WaitlistVerifyResponse,
)
from app.db.session import get_db
from app.services import waitlist_service
from app.services.waitlist_service import (
    ConsentVersionMismatchError,
    ConsentConfigurationError,
    TransactionBoundaryError,
    WaitlistServiceError,
    InvalidVerificationTokenError,
    VipCapacityInvariantError
)
from app.core.rate_limit_dependencies import (
    registration_rate_limit,
    verification_rate_limit,
    resend_verification_rate_limit,
)

router = APIRouter(
    prefix="/api/v1/waitlist",
    tags=["Waitlist"],
)

@router.post(
    "/register",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WaitlistRegisterResponse,
    dependencies=[Depends(registration_rate_limit)]
)
def register(
    payload: WaitlistRegisterRequest,
    db: Session = Depends(get_db)
) -> WaitlistRegisterResponse:
    """
    Registers a user for the waitlist.
    Protected by distributed Redis rate-limiting (fail-closed).
    """
    try:
        return waitlist_service.register_waitlist_entry(db=db, payload=payload)
    except ConsentVersionMismatchError:
        raise HTTPException(
            status_code=409,
            detail="The consent notice has changed. Reload the page and accept the current consent notice."
        )
    except ConsentConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="Registration is temporarily unavailable."
        )
    except TransactionBoundaryError:
        raise HTTPException(
            status_code=500,
            detail="An internal server error occurred."
        )
    except WaitlistServiceError:
        raise HTTPException(
            status_code=503,
            detail="Registration is temporarily unavailable."
        )


@router.post(
    "/verify",
    status_code=status.HTTP_200_OK,
    response_model=WaitlistVerifyResponse,
    dependencies=[Depends(verification_rate_limit)]
)
def verify(
    payload: WaitlistVerifyRequest,
    db: Session = Depends(get_db)
) -> WaitlistVerifyResponse:
    """
    Verifies a user's token and allocates VIP capacity via row-locking.
    Protected by distributed Redis rate-limiting (fail-closed).
    """
    try:
        return waitlist_service.verify_waitlist_email(db=db, raw_token=payload.token)
    except InvalidVerificationTokenError:
        raise HTTPException(
            status_code=400,
            detail="The verification link is invalid, expired, or has already been used."
        )
    except VipCapacityInvariantError:
        raise HTTPException(
            status_code=503,
            detail="Email verification is temporarily unavailable."
        )
    except TransactionBoundaryError:
        raise HTTPException(
            status_code=500,
            detail="An internal server error occurred."
        )
    except WaitlistServiceError:
        raise HTTPException(
            status_code=503,
            detail="Email verification is temporarily unavailable."
        )

@router.post(
    "/resend-verification",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WaitlistResendVerificationResponse,
)
def resend_verification(
    payload: WaitlistResendVerificationRequest = Depends(
        resend_verification_rate_limit
    ),
    db: Session = Depends(get_db),
) -> WaitlistResendVerificationResponse:
    """
    Request another verification email.

    The rate-limit dependency validates the request and enforces
    IP and per-email limits before the database dependency runs.
    """

    try:
        return waitlist_service.resend_verification_email(
            db=db,
            email=str(payload.email),
        )
    except TransactionBoundaryError:
        raise HTTPException(
            status_code=500,
            detail="An internal server error occurred.",
        ) from None
    except WaitlistServiceError:
        raise HTTPException(
            status_code=503,
            detail="Verification email is temporarily unavailable.",
        ) from None

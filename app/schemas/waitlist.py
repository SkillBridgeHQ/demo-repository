
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
)

class WaitlistRegisterRequest(BaseModel):
    """
    Client request payload for joining the waitlist.
    Enforces strict typing, length bounds, and explicit consent validation.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(
        ...,
        max_length=320,
        description="The user's email address. Whitespace is stripped.",
    )
    consent_accepted: StrictBool = Field(
        ...,
        description="Must be explicitly True to accept the privacy policy and terms.",
    )
    consent_version: StrictStr = Field(
        ...,
        min_length=1,
        max_length=50,
        description="The frontend-tracked version of the accepted consent document.",
    )

    @field_validator("email", mode="before")
    @classmethod
    def strip_email_whitespace(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("consent_accepted")
    @classmethod
    def require_true_consent(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Explicit consent must be provided to register.")
        return value

    @field_validator("consent_version", mode="before")
    @classmethod
    def require_non_empty_version(cls, value: str) -> str:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ValueError("Consent version cannot be blank.")
            return cleaned
        return value

class WaitlistRegisterResponse(BaseModel):
    """
    Generic 202 Accepted response for waitlist registration.
    Prevents callers from receiving state-specific registration responses.
    """

    model_config = ConfigDict(extra="forbid")

    message: Literal[
        "Your request has been received. If verification is required, you will receive an email shortly."
    ] = Field(
        default="Your request has been received. If verification is required, you will receive an email shortly.",
        description="A generic, immutable success message suitable for async outbox processing.",
    )

class WaitlistResendVerificationRequest(BaseModel):
    """
    Client request payload for requesting another verification email.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    email: EmailStr = Field(
        ...,
        max_length=320,
        description="The email address associated with the waitlist entry.",
    )

    @field_validator("email", mode="before")
    @classmethod
    def strip_email_whitespace(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value

class WaitlistResendVerificationResponse(BaseModel):
    """
    Generic response for verification-email resend requests.
    The same response must be returned whether the email is unknown,
    already verified, or eligible for another verification email.
    """

    model_config = ConfigDict(extra="forbid")

    message: Literal[
        "If an eligible waitlist entry exists, a verification email will be sent shortly."
    ] = Field(
        default="If an eligible waitlist entry exists, a verification email will be sent shortly.",
        description="A generic, immutable response that prevents account enumeration.",
    )

class WaitlistVerifyRequest(BaseModel):
    """
    Client request payload containing the single-use token from the email link.
    Hides sensitive input values in Pydantic validation errors.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    token: StrictStr = Field(
        ...,
        min_length=32,
        max_length=128,
        description="The raw URL-safe verification token.",
    )

    @field_validator("token")
    @classmethod
    def validate_urlsafe_token(cls, value: str) -> str:
        # secrets.token_urlsafe() generates base64url characters: A-Z, a-z, 0-9, -, _
        if not re.fullmatch(r"^[A-Za-z0-9\-_]+$", value):
            raise ValueError("Invalid token format.")
        return value

class WaitlistVerifyResponse(BaseModel):
    """
    Success response after validating the token, consuming it,
    and allocating VIP capacity.
    """

    model_config = ConfigDict(extra="forbid")

    verified: Literal[True] = Field(
        ...,
        description="Always explicitly True upon a successful 200 OK response.",
    )
    reference_code: StrictStr = Field(
        ...,
        min_length=1,
        max_length=32,
        description="The user's unique public identifier for share links.",
    )
    vip_number: StrictInt | None = Field(
        default=None,
        ge=1,
        le=500,
        description="The allocated early-access VIP slot (1-500). Null if capacity is exhausted.",
    )
    message: StrictStr = Field(
        ...,
        description="A success confirmation message detailing VIP status.",
    )

    @field_validator("reference_code", mode="before")
    @classmethod
    def strip_reference_code(cls, value: str) -> str:
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                raise ValueError("Reference code cannot be blank.")
            return cleaned
        return value
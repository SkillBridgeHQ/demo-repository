import base64
import binascii
import os
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

class EncryptionError(Exception):
    """Raised for any failure during encryption or decryption operations."""
    pass


TokenPurpose = Literal["verify", "delete"]
SUPPORTED_PURPOSES = ("verify", "delete")
ENVELOPE_DELIMITER = "."
APP_IDENTITY = "skillbridge-waitlist"


def _get_aesgcm_context() -> tuple[AESGCM, str]:
    """
    Retrieves the encryption key and active version from the application settings.
    Initializes and returns the AESGCM cipher context alongside the expected version string.
    """
    settings = get_settings()
    try:
        # Config validation ensures this is already valid Base64 and exactly 32 bytes
        key_bytes = base64.b64decode(settings.ENCRYPTION_KEY_V1.get_secret_value(), validate=True)
        return AESGCM(key_bytes), settings.ENCRYPTION_ACTIVE_KEY_VERSION
    except Exception:
        raise EncryptionError("Cryptographic engine failed to initialize.") from None


def _build_aad(version: str, purpose: TokenPurpose) -> bytes:
    """
    Constructs the Associated Authenticated Data (AAD) to bind the ciphertext
    to the application identity, version, and specific token purpose.
    """
    aad_string = f"{APP_IDENTITY}|{version}|{purpose}"
    return aad_string.encode("utf-8")


def _strict_urlsafe_b64decode(data: str) -> bytes:
    """
    Strictly decodes a URL-safe Base64 string.
    Rejects standard base64 characters, malformed padding, and non-canonical encodings.
    """
    if not isinstance(data, str):
        raise EncryptionError("Invalid encoding format.")

    # Python's b64decode allows standard characters even when altchars are provided.
    # We must explicitly reject them to ensure strictly URL-safe inputs.
    if "+" in data or "/" in data:
        raise EncryptionError("Invalid base64 encoding.")

    try:
        decoded_bytes = base64.b64decode(data, altchars=b"-_", validate=True)

        # Verify canonical encoding: re-encoding the bytes must produce the exact original string.
        # This catches non-zero padding bits and incorrect padding lengths.
        canonical_data = base64.urlsafe_b64encode(decoded_bytes).decode("ascii")
        if data != canonical_data:
            raise EncryptionError("Non-canonical base64 encoding.")

        return decoded_bytes
    except (binascii.Error, ValueError, UnicodeError):
        raise EncryptionError("Invalid base64 encoding.")


def encrypt_token(token: str, purpose: TokenPurpose) -> str:
    """
    Encrypts a plaintext token using AES-256-GCM and returns a versioned, URL-safe Base64 envelope.
    Binds the ciphertext cryptographically to the provided purpose using AAD.
    """
    if not token or not isinstance(token, str) or not token.strip():
        raise EncryptionError("Invalid plaintext token.")
    if purpose not in SUPPORTED_PURPOSES:
        raise EncryptionError("Invalid token purpose.")

    aesgcm, version = _get_aesgcm_context()
    nonce = os.urandom(12)  # Standard 96-bit nonce for GCM
    aad = _build_aad(version, purpose)

    try:
        ciphertext = aesgcm.encrypt(nonce, token.encode("utf-8"), aad)
    except Exception:
        raise EncryptionError("Encryption operation failed.") from None

    nonce_b64 = base64.urlsafe_b64encode(nonce).decode("ascii")
    ctx_b64 = base64.urlsafe_b64encode(ciphertext).decode("ascii")

    return f"{version}{ENVELOPE_DELIMITER}{nonce_b64}{ENVELOPE_DELIMITER}{ctx_b64}"


def decrypt_token(envelope: str, purpose: TokenPurpose) -> str:
    """
    Decrypts a versioned envelope and returns the plaintext token.
    Requires the exact purpose (AAD) used during encryption.
    """
    if not envelope or not isinstance(envelope, str):
        raise EncryptionError("Invalid ciphertext envelope.")
    if purpose not in SUPPORTED_PURPOSES:
        raise EncryptionError("Invalid token purpose.")

    parts = envelope.split(ENVELOPE_DELIMITER)
    if len(parts) != 3:
        raise EncryptionError("Malformed encryption envelope.")

    version, nonce_b64, ctx_b64 = parts
    aesgcm, active_version = _get_aesgcm_context()

    if version != active_version:
        raise EncryptionError("Unsupported encryption envelope version.")

    nonce = _strict_urlsafe_b64decode(nonce_b64)
    ciphertext = _strict_urlsafe_b64decode(ctx_b64)

    if len(nonce) != 12:
        raise EncryptionError("Invalid cryptographic parameters.")

    # Minimum length is 16 bytes (auth tag) + at least 1 byte of encrypted payload
    if len(ciphertext) <= 16:
        raise EncryptionError("Invalid cryptographic parameters.")

    aad = _build_aad(version, purpose)

    try:
        plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, aad)
    except InvalidTag:
        raise EncryptionError("Authentication failed.")
    except Exception:
        raise EncryptionError("Decryption operation failed.")

    try:
        return plaintext_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise EncryptionError("Authentication failed.")
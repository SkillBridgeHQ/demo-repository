import base64
import os
import secrets
import pytest

from app.core.config import get_settings
from app.core.encryption import (
    ENVELOPE_DELIMITER,
    EncryptionError,
    decrypt_token,
    encrypt_token,
)


@pytest.fixture(autouse=True)
def isolated_crypto_environment(monkeypatch, tmp_path):
    """
    Isolates the test environment completely.
    Changes directory to a temporary path so real .env files are ignored.
    Injects a fresh, random 32-byte AES key and development config values.
    """
    monkeypatch.chdir(tmp_path)

    fresh_key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
    monkeypatch.setenv("ENCRYPTION_KEY_V1", fresh_key)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EMAIL_MODE", "console")

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def raw_token():
    return secrets.token_urlsafe(32)


def test_encrypt_decrypt_verification(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    assert envelope != raw_token

    decrypted = decrypt_token(envelope, purpose="verify")
    assert decrypted == raw_token


def test_encrypt_decrypt_deletion(raw_token):
    envelope = encrypt_token(raw_token, purpose="delete")
    assert envelope != raw_token

    decrypted = decrypt_token(envelope, purpose="delete")
    assert decrypted == raw_token


def test_wrong_purpose_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    with pytest.raises(EncryptionError):
        decrypt_token(envelope, purpose="delete")


@pytest.mark.parametrize("invalid_purpose", ["", "invalid", "VERIFY", None])
def test_invalid_purpose_decryption(raw_token, invalid_purpose):
    envelope = encrypt_token(raw_token, purpose="verify")
    with pytest.raises(EncryptionError):
        decrypt_token(envelope, purpose=invalid_purpose)


def test_modified_ciphertext_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    # Modify a character in the middle to avoid altering base64 padding
    ctx_list = list(ctx)
    middle_idx = len(ctx_list) // 2
    ctx_list[middle_idx] = "a" if ctx_list[middle_idx] != "a" else "b"
    modified_ctx = "".join(ctx_list)

    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{nonce}{ENVELOPE_DELIMITER}{modified_ctx}"

    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_modified_nonce_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    nonce_list = list(nonce)
    middle_idx = len(nonce_list) // 2
    nonce_list[middle_idx] = "a" if nonce_list[middle_idx] != "a" else "b"
    modified_nonce = "".join(nonce_list)

    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{modified_nonce}{ENVELOPE_DELIMITER}{ctx}"

    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_invalid_base64_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    tampered_envelope = envelope + "!!!"
    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_standard_base64_characters_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    # Inject standard base64 characters (+, /) which must be rejected in our URL-safe context
    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{nonce}+{ENVELOPE_DELIMITER}{ctx}/"
    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_non_canonical_base64_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    # "AB==" decodes to \x00, but canonical re-encoding of \x00 is "AA=="
    non_canonical_nonce = "AB=="
    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{non_canonical_nonce}{ENVELOPE_DELIMITER}{ctx}"

    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_missing_base64_padding(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    # Strip '=' padding which makes it invalid strictly
    stripped_ctx = ctx.rstrip("=")

    # Explicitly assert that stripping the padding actually modified the string
    assert stripped_ctx != ctx, "Test token did not produce a padded ciphertext; cannot test missing padding."

    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{nonce}{ENVELOPE_DELIMITER}{stripped_ctx}"
    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_unsupported_encryption_version_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    tampered_envelope = f"v99{ENVELOPE_DELIMITER}{nonce}{ENVELOPE_DELIMITER}{ctx}"
    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


@pytest.mark.parametrize("malformed", [
    "v1.justtwoparts",
    "v1.nonce.ctx.extra",
    "nodotsatall"
])
def test_malformed_envelope_rejection(malformed):
    with pytest.raises(EncryptionError):
        decrypt_token(malformed, purpose="verify")


@pytest.mark.parametrize("empty_env", ["", None])
def test_empty_envelope_rejection(empty_env):
    with pytest.raises(EncryptionError):
        decrypt_token(empty_env, purpose="verify")


def test_empty_nonce_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, _, ctx = envelope.split(ENVELOPE_DELIMITER)

    tampered = f"{version}{ENVELOPE_DELIMITER}{ENVELOPE_DELIMITER}{ctx}"
    with pytest.raises(EncryptionError):
        decrypt_token(tampered, purpose="verify")


def test_empty_ciphertext_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, _ = envelope.split(ENVELOPE_DELIMITER)

    tampered = f"{version}{ENVELOPE_DELIMITER}{nonce}{ENVELOPE_DELIMITER}"
    with pytest.raises(EncryptionError):
        decrypt_token(tampered, purpose="verify")


def test_incorrect_nonce_length_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, ctx = envelope.split(ENVELOPE_DELIMITER)

    # 13 bytes instead of 12
    bad_nonce_bytes = os.urandom(13)
    bad_nonce = base64.urlsafe_b64encode(bad_nonce_bytes).decode("ascii")
    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{bad_nonce}{ENVELOPE_DELIMITER}{ctx}"

    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


def test_truncated_ciphertext_rejection(raw_token):
    envelope = encrypt_token(raw_token, purpose="verify")
    version, nonce, _ = envelope.split(ENVELOPE_DELIMITER)

    # 16 bytes is just the auth tag, meaning 0 bytes of ciphertext payload
    truncated_ctx_bytes = os.urandom(16)
    truncated_ctx = base64.urlsafe_b64encode(truncated_ctx_bytes).decode("ascii")
    tampered_envelope = f"{version}{ENVELOPE_DELIMITER}{nonce}{ENVELOPE_DELIMITER}{truncated_ctx}"

    with pytest.raises(EncryptionError):
        decrypt_token(tampered_envelope, purpose="verify")


@pytest.mark.parametrize("invalid_plaintext", [
    "",
    "   ",
    None
])
def test_invalid_plaintext_rejection(invalid_plaintext):
    with pytest.raises(EncryptionError):
        encrypt_token(invalid_plaintext, purpose="verify")


@pytest.mark.parametrize("invalid_purpose", [
    "invalid",
    "VERIFY",
    "",
    None
])
def test_invalid_token_purpose_rejection(raw_token, invalid_purpose):
    with pytest.raises(EncryptionError):
        encrypt_token(raw_token, purpose=invalid_purpose)


def test_incorrect_encryption_key_rejection(raw_token, monkeypatch):
    envelope = encrypt_token(raw_token, purpose="verify")

    # Simulate a key rotation or incorrect environment config
    new_key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("ENCRYPTION_KEY_V1", new_key)
    get_settings.cache_clear()

    with pytest.raises(EncryptionError):
        decrypt_token(envelope, purpose="verify")


def test_v2_encrypt_decrypt_verification(raw_token, monkeypatch):
    v2_key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("ENCRYPTION_KEY_V2", v2_key)
    monkeypatch.setenv("ENCRYPTION_ACTIVE_KEY_VERSION", "v2")
    get_settings.cache_clear()

    envelope = encrypt_token(raw_token, purpose="verify")
    assert envelope.startswith("v2.")
    assert decrypt_token(envelope, purpose="verify") == raw_token

def test_v1_envelope_remains_decryptable_when_v2_is_active(raw_token, monkeypatch):
    v1_envelope = encrypt_token(raw_token, purpose="verify")

    v2_key = base64.b64encode(os.urandom(32)).decode("ascii")
    monkeypatch.setenv("ENCRYPTION_KEY_V2", v2_key)
    monkeypatch.setenv("ENCRYPTION_ACTIVE_KEY_VERSION", "v2")
    get_settings.cache_clear()

    assert decrypt_token(v1_envelope, purpose="verify") == raw_token

def test_fresh_nonce_generation(raw_token):
    envelope1 = encrypt_token(raw_token, purpose="verify")
    envelope2 = encrypt_token(raw_token, purpose="verify")

    assert envelope1 != envelope2

    _, nonce1, ctx1 = envelope1.split(ENVELOPE_DELIMITER)
    _, nonce2, ctx2 = envelope2.split(ENVELOPE_DELIMITER)

    # The nonces must be completely unique
    assert nonce1 != nonce2
    # The ciphertexts must be completely unique due to the unique nonces
    assert ctx1 != ctx2
"""
Test environment bootstrap.

Provides safe configuration before test modules are imported.
These values exist only inside the pytest process.
No real database connection is established.
"""

import base64
import os


os.environ.setdefault("APP_ENV", "test")

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@127.0.0.1:5432/skillbridge_test",
)

_test_encryption_key = b"0123456789abcdef0123456789abcdef"

os.environ.setdefault(
    "ENCRYPTION_KEY_V1",
    base64.b64encode(_test_encryption_key).decode("ascii"),
)

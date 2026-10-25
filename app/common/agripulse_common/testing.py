"""Test and scan support: a throwaway RSA key pair and real tokens, so tests and the DAST scan exercise the same
sign-in code as production (there is no "test user" switch in the services).

    python -m agripulse_common.testing keys            # export lines for JWT_PRIVATE_KEY / JWT_PUBLIC_KEY
    python -m agripulse_common.testing token <role>    # a token for a test user with that role (needs JWT_PRIVATE_KEY)
"""

import os
import shlex
import sys
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from . import auth


def key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def test_environment(**extra) -> dict[str, str]:
    """Everything a service needs to run without AWS: SQLite, keys, secrets, plain-HTTP cookies."""
    private, public = key_pair()
    env = {"DATABASE_URL": "sqlite://", "JWT_PRIVATE_KEY": private, "JWT_PUBLIC_KEY": public, "JWT_ISSUER": "agripulse-test",
           "CSRF_SECRET": "test-csrf-secret", "INTERNAL_TOKEN": "test-internal-token", "COOKIE_SECURE": "false",
           "ENVIRONMENT": "test", "ACCOUNTS_URL": "http://accounts.test", "FARM_URL": "http://farm.test",
           "SHOP_URL": "http://shop.test", "INCIDENTS_URL": "http://incidents.test", "PUBLIC_URL": "http://testserver"}
    env.update(extra)
    return env


def token(role: str, *, user_id: str | None = None, email: str | None = None, name: str | None = None) -> str:
    user_id = user_id or str(uuid.uuid4())
    return auth.issue_token(os.environ["JWT_PRIVATE_KEY"], user_id=user_id, email=email or f"{role}-{user_id[:8]}@example.com",
                            name=name or f"Test {role}", role=role)


def main(argv: list[str]) -> int:
    if argv[:1] == ["keys"]:
        private, public = key_pair()
        print(f"export JWT_PRIVATE_KEY={shlex.quote(private)}\nexport JWT_PUBLIC_KEY={shlex.quote(public)}")
        return 0
    if len(argv) == 2 and argv[0] == "token":
        print(token(argv[1], user_id="00000000-0000-4000-8000-000000000001"))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

import base64
import hashlib
import logging

from cryptography.fernet import Fernet

from app.config import get_settings

logger = logging.getLogger(__name__)


def _derive_key(raw_key: str) -> bytes:
    if not raw_key:
        logger.warning(
            "APP_ENCRYPTION_KEY is not set. Using a hardcoded insecure key — "
            "MCP credentials are NOT protected. Set APP_ENCRYPTION_KEY before storing any secrets."
        )
        raw_key = "local-dev-insecure-key"
    if len(raw_key) == 44 and raw_key.endswith("="):
        try:
            Fernet(raw_key.encode("utf-8"))
            return raw_key.encode("utf-8")
        except Exception:
            pass
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecretBox:
    def __init__(self) -> None:
        settings = get_settings()
        self._fernet = Fernet(_derive_key(settings.app_encryption_key))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("utf-8")).decode("utf-8")


secret_box = SecretBox()


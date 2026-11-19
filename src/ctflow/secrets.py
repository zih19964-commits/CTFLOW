"""基于操作系统原生凭据管理器（keyring）的密钥存取。"""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

SERVICE_NAME = "ctflow"


class SecretBox:
    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service

    def get(self, key: str) -> str:
        return keyring.get_password(self.service, key) or ""

    def set(self, key: str, secret: str) -> None:
        if secret:
            keyring.set_password(self.service, key, secret)
        else:
            self.delete(key)

    def delete(self, key: str) -> None:
        try:
            keyring.delete_password(self.service, key)
        except PasswordDeleteError:
            pass

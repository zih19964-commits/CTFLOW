"""测试全局夹具：为每个测试函数挂载独立内存 keyring 后端，隔离本机系统凭据管理器。"""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class _InMemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._data:
            raise PasswordDeleteError(username)
        del self._data[(service, username)]


@pytest.fixture(autouse=True)
def _isolated_keyring() -> None:
    keyring.set_keyring(_InMemoryKeyring())

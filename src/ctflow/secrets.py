"""跨平台的本机凭据加密。

Windows 默认使用 DPAPI；其他平台使用 Fernet，并把随机主密钥放在
数据目录的 ``.ctflow.key``。两种密文都带前缀，读取时按前缀解密。
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_DPAPI_PREFIX = "dpapi:v1:"
_FERNET_PREFIX = "fernet:v1:"
_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt32():
    if sys.platform != "win32":
        raise RuntimeError("当前平台不支持 Windows DPAPI")
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    library.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    library.CryptProtectData.restype = wintypes.BOOL
    library.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    library.CryptUnprotectData.restype = wintypes.BOOL
    return library


def _take_blob(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(blob.pbData)


def _dpapi_protect(text: str) -> str:
    library = _crypt32()
    source, _source_buffer = _blob(text.encode("utf-8"))
    target = _DataBlob()
    if not library.CryptProtectData(
        ctypes.byref(source),
        "CTflow Worker API Key",
        None,
        None,
        None,
        _UI_FORBIDDEN,
        ctypes.byref(target),
    ):
        raise OSError(ctypes.get_last_error(), "DPAPI 加密 API Key 失败")
    return _DPAPI_PREFIX + base64.b64encode(_take_blob(target)).decode("ascii")


def _dpapi_unprotect(value: str) -> str:
    library = _crypt32()
    try:
        encrypted = base64.b64decode(value[len(_DPAPI_PREFIX) :], validate=True)
    except ValueError as error:
        raise ValueError("DPAPI 密文格式无效，请重新填写 API Key") from error
    source, _source_buffer = _blob(encrypted)
    target = _DataBlob()
    if not library.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _UI_FORBIDDEN,
        ctypes.byref(target),
    ):
        raise ValueError("无法解密 API Key；密文可能来自其他 Windows 用户或已损坏")
    return _take_blob(target).decode("utf-8")


class SecretBox:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        selected = os.environ.get("CTFLOW_SECRET_BACKEND", "auto").lower()
        if selected == "auto":
            selected = "dpapi" if sys.platform == "win32" else "fernet"
        if selected not in {"dpapi", "fernet"}:
            raise ValueError("CTFLOW_SECRET_BACKEND 只能是 auto、dpapi 或 fernet")
        if selected == "dpapi" and sys.platform != "win32":
            raise RuntimeError("DPAPI 仅在 Windows 可用")
        self.backend = selected

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return value.startswith((_DPAPI_PREFIX, _FERNET_PREFIX))

    def _fernet(self, *, create: bool) -> Fernet:
        path = self.data_dir / ".ctflow.key"
        if not path.exists():
            if not create:
                raise ValueError("缺少本机 Fernet 主密钥 data/.ctflow.key")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
        try:
            return Fernet(path.read_bytes())
        except (OSError, ValueError) as error:
            raise ValueError("无法读取本机 Fernet 主密钥，请检查 .ctflow.key") from error

    def protect(self, text: str) -> str:
        if not text or self.is_encrypted(text):
            return text
        if self.backend == "dpapi":
            return _dpapi_protect(text)
        token = self._fernet(create=True).encrypt(text.encode("utf-8"))
        return _FERNET_PREFIX + token.decode("ascii")

    def unprotect(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith(_DPAPI_PREFIX):
            return _dpapi_unprotect(value)
        if value.startswith(_FERNET_PREFIX):
            try:
                return (
                    self._fernet(create=False)
                    .decrypt(value[len(_FERNET_PREFIX) :].encode("ascii"))
                    .decode("utf-8")
                )
            except (InvalidToken, ValueError) as error:
                raise ValueError("无法解密 Fernet 密文，请检查 .ctflow.key") from error
        return value

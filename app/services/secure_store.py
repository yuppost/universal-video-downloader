"""Encryption at rest for sign-in cookies.

* Windows: DPAPI (``CryptProtectData``) - the data can only be decrypted by the *same Windows user on the same PC*.
  Copying the file to another PC/account (or reading it as another user) gives only unreadable bytes. No key file.
* Linux/macOS: Fernet (AES-128-CBC + HMAC) with a random key kept in a 0600 file next to the data. This stops casual
  reading, backups/cloud-sync leaks and copying a single file, but not someone who can read your whole home folder.
* Anything without the magic header is treated as legacy plain text so old installs migrate automatically.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

MAGIC_WIN = b"UVSC1W\n"
MAGIC_FERNET = b"UVSC1F\n"
_ENTROPY = b"UniversalVideoStudio.cookies.v1"


class SecureStoreError(Exception):
    pass


# ---------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    src, _k1 = blob(data)
    ent, _k2 = blob(_ENTROPY)
    out = BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # CryptProtectData(in, descr, entropy, reserved, prompt, flags, out) / CryptUnprotectData(same shape)
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x01, ctypes.byref(out)):
        raise SecureStoreError("Windows could not " + ("protect" if protect else "unprotect") + " the cookie data.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- Fernet (non-Windows)
def _fernet(key_dir: Path):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:                       # pragma: no cover - dependency missing
        raise SecureStoreError("The 'cryptography' package is missing.") from exc
    key_file = Path(key_dir) / ".key"
    try:
        key = key_file.read_bytes().strip()
        if not key:
            raise OSError
    except OSError:
        key = Fernet.generate_key()
        key_dir = Path(key_dir)
        key_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    return Fernet(key)


# ---------------------------------------------------------------- public API
def protect(data: bytes, key_dir: Optional[Path] = None) -> bytes:
    if sys.platform == "win32":
        return MAGIC_WIN + _dpapi(data, True)
    return MAGIC_FERNET + _fernet(key_dir or Path(".")).encrypt(data)


def unprotect(blob: bytes, key_dir: Optional[Path] = None) -> bytes:
    """Decrypt, or return legacy plain data unchanged."""
    if blob.startswith(MAGIC_WIN):
        if sys.platform != "win32":
            raise SecureStoreError("These cookies were encrypted by Windows and can only be opened there.")
        return _dpapi(blob[len(MAGIC_WIN):], False)
    if blob.startswith(MAGIC_FERNET):
        try:
            return _fernet(key_dir or Path(".")).decrypt(blob[len(MAGIC_FERNET):])
        except SecureStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrong key / corrupted file
            raise SecureStoreError("The saved cookies cannot be decrypted (wrong key or damaged file).") from exc
    return blob


def is_protected(blob: bytes) -> bool:
    return blob.startswith((MAGIC_WIN, MAGIC_FERNET))

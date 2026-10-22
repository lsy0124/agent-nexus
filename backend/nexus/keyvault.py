"""Provider keys: memory by default, Windows current-user DPAPI by explicit opt-in.

Ciphertext lives in the project ignored work/secrets directory. No ACL or Windows
configuration is changed. The API exposes fingerprint/length only. Authentication
files belonging to other agents are never read. Persistence failures are explicit.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

BACKEND_MEMORY = "memory"
BACKEND_DPAPI = "dpapi"
BACKEND_ENV = "NEXUS_KEY_VAULT"
SECRET_DIR_ENV = "NEXUS_SECRET_DIR"
APP_DIR_NAME = "AgentNexus"
SCHEMA_VERSION = 1

# CRYPTPROTECT_UI_FORBIDDEN: never show a Windows prompt from a background process.
CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Verify the running account still matches what encrypted the blob.
ERROR_INVALID_DATA = 13


class VaultUnavailable(RuntimeError):
    """DPAPI is not reachable, or a specific blob cannot be decrypted."""


class VaultLoadError(RuntimeError):
    """A stored key exists but could not be loaded; the user must re-authorize."""

    def __init__(self, message: str, provider_id: str, reason: str):
        self.provider_id, self.reason = provider_id, reason
        super().__init__(message)


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt32():
    """A crypt32 handle that captures ``GetLastError`` for us.

    ``ctypes.windll`` loads with ``use_last_error=False``, so ``get_last_error()``
    would read a stale value and report a meaningless error code. Loading with
    ``use_last_error=True`` makes the reported failure real.
    """
    return ctypes.WinDLL("crypt32", use_last_error=True)


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def dpapi_available() -> bool:
    """Whether this interpreter can reach the Windows DPAPI entry points."""
    if os.name != "nt":
        return False
    try:
        return bool(_crypt32().CryptProtectData) and bool(_kernel32().LocalFree)
    except (AttributeError, OSError):
        return False


def _to_blob(data: bytes) -> "_DataBlob":
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _protect(plaintext: bytes) -> bytes:
    """Encrypt for the current Windows user."""
    if not dpapi_available():
        raise VaultUnavailable("当前环境不支持 Windows DPAPI")
    crypt32, kernel32 = _crypt32(), _kernel32()
    blob_in, blob_out = _to_blob(plaintext), _DataBlob()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise VaultUnavailable(f"DPAPI 加密失败（GetLastError={ctypes.get_last_error()}）")
    try:
        return _from_blob(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _unprotect(ciphertext: bytes) -> bytes:
    """Decrypt a blob produced by :func:`_protect` for this Windows user."""
    if not dpapi_available():
        raise VaultUnavailable("当前环境不支持 Windows DPAPI")
    crypt32, kernel32 = _crypt32(), _kernel32()
    blob_in, blob_out = _to_blob(ciphertext), _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                      CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        code = ctypes.get_last_error()
        # DPAPI is bound to the Windows account and machine that encrypted the
        # blob. A different account cannot read it, and that is the design.
        raise VaultUnavailable(
            f"DPAPI 解密失败（GetLastError={code}）：该密文由其他 Windows 用户或其他计算机加密，"
            "当前账户无法解密。请重新输入该 Provider 的 API Key。")
    try:
        return _from_blob(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def fingerprint(secret: str) -> str:
    """Non-reversible identifier so the UI can confirm *which* key is loaded."""
    return hashlib.sha256(("nexus-key:" + secret).encode("utf-8")).hexdigest()[:12]


def default_secret_dir() -> Path:
    """The current Windows user's private application data directory."""
    override = os.environ.get(SECRET_DIR_ENV)
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_DIR_NAME / "secrets"
    # Non-Windows or a stripped environment: stay inside the user profile.
    return Path.home() / f".{APP_DIR_NAME.lower()}" / "secrets"


def _inside_git_worktree(path: Path) -> bool:
    """Whether a directory lives inside any Git working tree."""
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return True
    return False


class SecretVault:
    """Provider-key store with an explicit, reportable backend."""

    def __init__(self, root: Path, backend: str | None = None, directory: Path | None = None):
        self.root = root.resolve()
        requested = (backend or os.environ.get(BACKEND_ENV) or "memory").strip().lower()
        self.requested = requested or "auto"
        self.fallback_reason: str | None = None
        self.load_errors: list[dict] = []

        if not requested:
            # Default: DPAPI wherever it is genuinely available.
            self.backend = BACKEND_DPAPI if dpapi_available() else BACKEND_MEMORY
            if self.backend == BACKEND_MEMORY:
                self.fallback_reason = ("当前环境不支持 Windows DPAPI，已使用仅内存保存；"
                                        "服务重启后需要重新提供密钥。")
        elif requested == BACKEND_DPAPI and not dpapi_available():
            # Never pretend to protect a key: refuse the promise, and say so.
            self.backend = BACKEND_MEMORY
            self.fallback_reason = "请求了 DPAPI，但当前环境不支持；已回退为仅内存保存。"
        elif requested in (BACKEND_MEMORY, BACKEND_DPAPI):
            self.backend = requested
        else:
            self.backend = BACKEND_MEMORY
            self.fallback_reason = f"未知的密钥后端 {requested!r}；已回退为仅内存保存。"

        self.directory = (directory or Path(os.environ.get(SECRET_DIR_ENV) or self.root / 'work' / 'secrets')).expanduser().absolute()
        self._memory: dict[str, str] = {}
        self.acl: dict = {"applied": False, "reason": "未使用磁盘后端"}
        if self.backend == BACKEND_DPAPI:
            if not self.directory.resolve().is_relative_to(self.root) or self.directory.resolve() != self.directory:
                raise ValueError('密钥写入目录必须位于当前项目内，且不能经符号链接或 junction 重定向')
            if (self.root / '.git').exists() and not self.directory.resolve().is_relative_to(self.root / 'work'):
                # A ciphertext file inside a repository can be committed by
                # accident. The check is skipped only for an explicit
                # NEXUS_SECRET_DIR, which is how an isolated test run points the
                # vault at its own disposable directory.
                raise ValueError(
                    f"密钥目录不能位于 Git 工作树内：{self.directory}。"
                    "请使用当前 Windows 用户的应用数据目录，"
                    f"或以 {SECRET_DIR_ENV} 显式指定一个隔离目录。")
            self.acl = {"applied": False, "reason": "目录尚未创建"}
            self._restore()

    # ------------------------------------------------------------------ paths

    def _path(self, provider_id: str) -> Path:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', provider_id):
            raise ValueError("Provider 标识无效")
        path = self.directory / (provider_id + ".dpapi")
        if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
            raise ValueError('密钥文件不能是符号链接或硬链接')
        return path

    def _ensure_directory(self) -> None:
        """Create the secret directory if needed and always (re)apply the ACL.

        The ACL is applied on every write rather than only on creation, so a
        directory that already existed with inherited permissions is tightened
        too, and a later permission change is corrected on the next save.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        self.acl = {'applied': False, 'reason': '未修改 Windows ACL；保密性由当前用户 DPAPI 加密提供'}

    def _restore(self) -> None:
        """Reload persisted keys so a restart does not lose configuration."""
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.dpapi")):
            provider_id = path.stem
            try:
                self._path(provider_id)
                raw = path.read_bytes()
            except (OSError, ValueError) as exc:
                self.load_errors.append({"provider_id": provider_id, "reason": f"读取失败：{type(exc).__name__}"})
                continue
            try:
                document = json.loads(_unprotect(raw).decode("utf-8"))
                if document.get('provider_id') != provider_id or document.get('schema') != SCHEMA_VERSION:
                    raise ValueError('密钥文档身份或版本不匹配')
                secret = document["secret"]
                if not isinstance(secret, str) or not secret:
                    raise ValueError("空密钥")
            except VaultUnavailable as exc:
                # Reported, never guessed at, never overwritten with plaintext.
                self.load_errors.append({"provider_id": provider_id, "reason": str(exc)})
                continue
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
                self.load_errors.append({"provider_id": provider_id,
                                         "reason": "密文格式无法识别；请重新输入该 Provider 的 API Key。"})
                continue
            self._memory[provider_id] = secret

    # ----------------------------------------------------------------- public

    def set(self, provider_id: str, secret: str) -> dict:
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("密钥不能为空")
        if self.backend == BACKEND_DPAPI:
            self._ensure_directory()
            document = json.dumps({
                "schema": SCHEMA_VERSION,
                "provider_id": provider_id,
                "secret": secret,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "fingerprint": fingerprint(secret),
            }, ensure_ascii=False).encode("utf-8")
            path = self._path(provider_id)
            # Write then replace atomically, so an interrupted write cannot leave
            # a half-written blob that would look like a decryption failure.
            import uuid
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            encrypted = _protect(document)
            with temporary.open('xb') as stream:
                stream.write(encrypted)
            os.replace(temporary, path)
            self.load_errors = [item for item in self.load_errors if item["provider_id"] != provider_id]
        self._memory[provider_id] = secret
        return self.describe(provider_id)

    def get(self, provider_id: str) -> str | None:
        return self._memory.get(provider_id)

    def clear(self, provider_id: str) -> dict:
        self._memory.pop(provider_id, None)
        if self.backend == BACKEND_DPAPI:
            try:
                self._path(provider_id).unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                raise VaultUnavailable('无法删除持久化密钥；未声称删除成功') from exc
        self.load_errors = [item for item in self.load_errors if item["provider_id"] != provider_id]
        return self.describe(provider_id)

    def describe(self, provider_id: str) -> dict:
        """Everything the API may reveal about a stored key."""
        secret = self._memory.get(provider_id)
        error = next((item for item in self.load_errors if item["provider_id"] == provider_id), None)
        return {
            "provider_id": provider_id,
            "configured": secret is not None,
            "fingerprint": fingerprint(secret) if secret else None,
            "length": len(secret) if secret else None,
            "backend": self.backend,
            "persisted": bool(secret) and self.backend == BACKEND_DPAPI,
            "load_error": error["reason"] if error else None,
        }

    def status(self) -> dict:
        persisted = sorted(path.stem for path in self.directory.glob("*.dpapi")) \
            if self.backend == BACKEND_DPAPI and self.directory.exists() else []
        return {
            "backend": self.backend,
            "requested_backend": self.requested,
            "dpapi_available": dpapi_available(),
            "persists_across_restart": self.backend == BACKEND_DPAPI,
            "directory": str(self.directory) if self.backend == BACKEND_DPAPI else None,
            "outside_git_worktree": (not _inside_git_worktree(self.directory)) if self.backend == BACKEND_DPAPI else None,
            "acl": self.acl,
            "fallback_reason": self.fallback_reason,
            "load_errors": list(self.load_errors),
            "stored_providers": persisted,
            "configured_providers": sorted(self._memory),
            "account": os.environ.get("USERNAME"),
            "note": ("密钥只在后端使用。接口只返回指纹与长度，绝不返回密钥本身或可读片段；"
                     "日志与事件中不记录 Authorization。"
                     + (f"当前为 Windows DPAPI 保护，密文位于 {self.directory}，重启后自动加载。"
                        if self.backend == BACKEND_DPAPI else
                        "当前为仅内存保存，服务重启后必须重新提供。")),
            "limitation": ("DPAPI 按 Windows 用户与计算机加密：更换运行账户或换机后无法解密，"
                           "此时必须重新输入密钥重新授权。"),
        }

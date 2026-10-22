"""Only Nexus writes model-generated text, after validating every destination.

The model cannot run a shell. This is a constrained file-edit bridge, not an OS sandbox.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import stat

MAX_FILES, MAX_BYTES = 20, 256_000
EXTENSIONS = {".txt", ".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".css",
              ".html", ".svg", ".toml", ".yaml", ".yml", ".ini", ".csv"}
RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
SENSITIVE = re.compile(r"(?i)(^\.env($|\.)|credentials?|cookies?|tokens?|secrets?|^auth\.|\.pem$|\.key$|^id_rsa)")
BLOCKED_PARTS = {".git", ".codex", ".agents", ".aws", ".ssh", ".venv", "node_modules", "work"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 220:
        raise ValueError("文件路径为空或过长")
    if "\\" in value or any(ord(c) < 32 for c in value) or any(c in value for c in ':*?"<>|'):
        raise ValueError("文件路径包含 Windows 不安全字符")
    parts = value.split("/")
    if PureWindowsPath(value).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise ValueError("只能使用工作目录内的相对文件路径")
    for part in parts:
        if part.endswith((".", " ")) or part.split(".")[0].casefold() in RESERVED:
            raise ValueError("拒绝 Windows 保留文件名")
        if part.casefold() in BLOCKED_PARTS or SENSITIVE.search(part):
            raise ValueError("拒绝私有数据、凭证、Git 元数据或运行目录")
        if part.startswith(".") and part != ".gitignore":
            raise ValueError("此桥接不允许隐藏文件")
    if value != ".gitignore" and Path(value).suffix.lower() not in EXTENSIONS:
        raise ValueError("文件类型未获准；可执行文件与脚本安装器需要单独授权")
    return value


def checked_path(root: Path, relative: str) -> Path:
    relative_path(relative)
    root = root.resolve()
    target = root.joinpath(*relative.split("/"))
    if not target.resolve().is_relative_to(root):
        raise ValueError("文件路径逃逸工作目录")
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("拒绝符号链接或 Windows reparse point")
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                raise ValueError("拒绝可能指向白名单外的硬链接")
    return target


def context_files(root: Path, names: list[str]) -> tuple[list[dict], dict[str, str]]:
    if len(names) > MAX_FILES or len(set(n.casefold() for n in names)) != len(names):
        raise ValueError("输入文件过多或重复")
    files, hashes, total = [], {}, 0
    for name in names:
        path = checked_path(root, name)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError(f"输入文件不存在或过大：{name}")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_BYTES * 2:
            raise ValueError("输入文件总量超过限制")
        files.append({"path": name, "content": data.decode("utf-8")})
        hashes[name] = sha(data)
    return files, hashes


def validate_files(root: Path, proposals: list[dict], input_hashes: dict[str, str]) -> list[tuple]:
    """No writes: preflight uses exactly the same policy as the final writer."""
    if len(proposals) > MAX_FILES:
        raise ValueError("Codex 返回的文件数超过限制")
    pending, seen, total = [], set(), 0
    # Validate the complete batch before performing any write.
    for proposal in proposals:
        name, content = proposal["path"], proposal["content"]
        path = checked_path(root, name)
        key = name.casefold()
        if key in seen:
            raise ValueError("Codex 返回了重复或大小写冲突的文件")
        seen.add(key)
        data = content.encode("utf-8")
        total += len(data)
        if len(data) > MAX_BYTES or total > MAX_BYTES * 2:
            raise ValueError("Codex 返回的文件内容过大")
        old = None
        if path.exists():
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError("目标不是受支持的普通文件")
            old = path.read_bytes()
            if input_hashes.get(name) != sha(old):
                raise ValueError(f"覆盖现有文件必须先选择该文件作为输入，且内容不能在执行中改变：{name}")
        pending.append((name, path, data, old))
    return pending


def apply_files(root: Path, proposals: list[dict], input_hashes: dict[str, str]) -> list[dict]:
    pending = validate_files(root, proposals, input_hashes)
    written, created_dirs = [], []
    try:
        for name, path, data, old in pending:
            checked_path(root, name)
            missing = []
            parent = path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for folder in reversed(missing):
                folder.mkdir()
                created_dirs.append(folder)
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
            if old is None:
                flags |= os.O_EXCL
            else:
                if sha(path.read_bytes()) != input_hashes[name]:
                    raise ValueError("目标文件在落盘前发生变更")
                flags |= os.O_TRUNC
            with os.fdopen(os.open(path, flags, 0o600), "wb") as handle:
                written.append((path, old))
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if path.read_bytes() != data:
                raise ValueError("文件落盘后内容校验失败")
        return [{"path": name, "sha256": sha(data), "size": len(data),
                 "generated_by": "codex-cli", "written_by": "nexus-validated-writer"}
                for name, _, data, _ in pending]
    except BaseException:
        # Roll back only this batch's owned writes, never arbitrary user paths.
        for path, old in reversed(written):
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old)
        for folder in reversed(created_dirs):
            try:
                folder.rmdir()
            except OSError:
                pass
        raise

"""Resource and filesystem guards for untrusted marketplace-source clones (Phase 1, Task 3).

Everything here is fail-closed: any trip raises `GuardViolationError` and the whole source scan is
rejected -- never partial trust. Guards cover total size, file count, per-file size, path depth,
symlink escape (absolute or resolving outside the clone root), submodules, and decompression
bombs (zip/tar/gz/bz2/xz expansion size + ratio). Archives are inspected, never extracted.
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

_CHUNK = 1 << 20
_ZIP_SUFFIXES = (".zip", ".whl", ".jar", ".nupkg", ".crate")
_TAR_SUFFIXES = (".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz")
_STREAM_OPENERS = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}
SYMLINK_MODE = "120000"
SUBMODULE_MODE = "160000"


class GuardViolationError(Exception):
    """A resource/safety guard tripped; `reason` is a stable machine-checkable code."""

    def __init__(self, reason: str, detail: str) -> None:
        """Store the machine-checkable `reason` with a human-readable `detail`."""
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(slots=True, frozen=True)
class GuardLimits:
    """Immutable scan limits (defaults are conservative; overridable from config)."""

    max_total_bytes: int = 100 * 1024 * 1024
    max_files: int = 5000
    max_file_bytes: int = 25 * 1024 * 1024
    max_depth: int = 12
    clone_timeout_s: float = 120.0
    max_archive_expanded_bytes: int = 200 * 1024 * 1024
    max_archive_ratio: int = 100
    max_archive_entries: int = 5000


def check_listing(entries: list[tuple[str, int, str]], limits: GuardLimits) -> None:
    """Pre-checkout guard over `git ls-tree -r -l` rows `(mode, size, path)`."""
    if len(entries) > limits.max_files:
        raise GuardViolationError("too_many_files", f"{len(entries)} > {limits.max_files}")
    total = 0
    for mode, size, path in entries:
        if mode == SUBMODULE_MODE:
            raise GuardViolationError("submodule", "git submodules are not allowed")
        if path.startswith("/") or ".." in path.split("/"):
            raise GuardViolationError("path_traversal", "unsafe path in tree")
        if len(path.split("/")) > limits.max_depth:
            raise GuardViolationError("too_deep", f"depth > {limits.max_depth}")
        if size > limits.max_file_bytes:
            raise GuardViolationError("file_too_large", f"{size} > {limits.max_file_bytes}")
        total += size
        if total > limits.max_total_bytes:
            raise GuardViolationError("clone_too_large", f"> {limits.max_total_bytes} bytes")


def dir_size(root: Path) -> int:
    """Best-effort on-disk byte total of `root` (no symlink following), for the clone monitor."""
    total = 0
    for base, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


def _check_symlink(path: str, root_real: str) -> None:
    """Reject absolute symlinks and any whose resolution leaves the clone root."""
    target = os.readlink(path)
    if os.path.isabs(target):
        raise GuardViolationError("symlink_absolute", "absolute symlink target")
    resolved = os.path.realpath(path)
    if resolved != root_real and not resolved.startswith(root_real + os.sep):
        raise GuardViolationError("symlink_escape", "symlink resolves outside the clone root")


def _check_stream(path: str, opener: object, limits: GuardLimits) -> None:
    """Stream-decompress `path` with a hard byte cap and ratio check (bomb guard)."""
    compressed = max(os.path.getsize(path), 1)
    expanded = 0
    try:
        with opener(path, "rb") as fh:  # type: ignore[operator]  # noqa: S110 - bounded read
            while chunk := fh.read(_CHUNK):
                expanded += len(chunk)
                if expanded > limits.max_archive_expanded_bytes:
                    raise GuardViolationError("archive_bomb", "expanded size cap exceeded")
                if expanded // compressed > limits.max_archive_ratio and expanded > _CHUNK:
                    raise GuardViolationError("archive_bomb", "decompression ratio exceeded")
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise GuardViolationError("archive_corrupt", "unreadable compressed file") from exc


def _check_zip(path: str, limits: GuardLimits) -> None:
    """Check zip central-directory sizes/ratio without extracting."""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise GuardViolationError("archive_corrupt", "unreadable zip") from exc
    if len(infos) > limits.max_archive_entries:
        raise GuardViolationError("archive_bomb", "too many zip entries")
    expanded = sum(i.file_size for i in infos)
    compressed = max(sum(i.compress_size for i in infos), 1)
    if expanded > limits.max_archive_expanded_bytes:
        raise GuardViolationError("archive_bomb", "zip expanded size cap exceeded")
    if expanded // compressed > limits.max_archive_ratio and expanded > _CHUNK:
        raise GuardViolationError("archive_bomb", "zip decompression ratio exceeded")
    for info in infos:
        parts = info.filename.split("/")
        if info.filename.startswith("/") or ".." in parts:
            raise GuardViolationError("path_traversal", "unsafe path inside zip")


def _check_tar(path: str, limits: GuardLimits) -> None:
    """Walk tar headers (streamed, bounded) checking size, count and unsafe members."""
    expanded = 0
    count = 0
    compressed = max(os.path.getsize(path), 1)
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf:
                count += 1
                expanded += max(member.size, 0)
                if count > limits.max_archive_entries or expanded > (
                    limits.max_archive_expanded_bytes
                ):
                    raise GuardViolationError("archive_bomb", "tar entry/size cap exceeded")
                parts = member.name.split("/")
                if member.name.startswith("/") or ".." in parts or member.issym() or member.islnk():
                    raise GuardViolationError("path_traversal", "unsafe member inside tar")
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError) as exc:
        raise GuardViolationError("archive_corrupt", "unreadable tar") from exc
    if expanded // compressed > limits.max_archive_ratio and expanded > _CHUNK:
        raise GuardViolationError("archive_bomb", "tar decompression ratio exceeded")


def _check_archive(path: str, limits: GuardLimits) -> None:
    """Dispatch an archive-looking file to the matching bomb guard."""
    lower = path.lower()
    if lower.endswith(_ZIP_SUFFIXES):
        _check_zip(path, limits)
    elif lower.endswith(_TAR_SUFFIXES):
        _check_tar(path, limits)
    else:
        for suffix, opener in _STREAM_OPENERS.items():
            if lower.endswith(suffix):
                _check_stream(path, opener, limits)
                return


def check_tree(root: Path, limits: GuardLimits) -> int:
    """Walk the checked-out clone enforcing every guard; returns the file count examined."""
    root_real = os.path.realpath(root)
    files = 0
    total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        if ".git" in dirs:
            dirs.remove(".git")
        rel = os.path.relpath(base, root)
        depth = 0 if rel == "." else len(rel.split(os.sep))
        if depth > limits.max_depth:
            raise GuardViolationError("too_deep", f"depth > {limits.max_depth}")
        for name in [*dirs, *names]:
            full = os.path.join(base, name)
            if os.path.islink(full):
                _check_symlink(full, root_real)
        for name in names:
            full = os.path.join(base, name)
            if os.path.islink(full):
                continue
            files += 1
            if files > limits.max_files:
                raise GuardViolationError("too_many_files", f"> {limits.max_files}")
            size = os.lstat(full).st_size
            if size > limits.max_file_bytes:
                raise GuardViolationError("file_too_large", f"{size} > {limits.max_file_bytes}")
            total += size
            if total > limits.max_total_bytes:
                raise GuardViolationError("clone_too_large", f"> {limits.max_total_bytes} bytes")
            _check_archive(full, limits)
    return files

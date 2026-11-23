"""Create independent artifacts atomically without replacing earlier output."""
from __future__ import annotations

import os
import hashlib
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from suwan.adapters.filestore import DataFileError, ensure_dir


def file_fingerprint(path: str | Path) -> bytes | None:
    """Fingerprint includes absence; unreadable files never look like new stores."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").digest()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DataFileError(path, f"读取失败（{type(exc).__name__}）") from exc


def file_revision(path: str | Path) -> tuple | None:
    """Extra snapshot identity; timestamps alone never authorize cached contents."""
    path = Path(path)
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DataFileError(path, f"读取文件状态失败（{type(exc).__name__}）") from exc


def check_pending_commit(path: str | Path) -> None:
    """A crash after claiming the old file must not look like first initialization."""
    path = Path(path)
    if file_revision(path) is None:
        recoveries = list(path.parent.glob(f".{path.name}.commit-*/original"))
        if recoveries:
            raise DataFileError(path, "上次提交未完成，请先恢复保留的原文件：" + "，".join(map(str, recoveries)))


def _publish_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)  # Windows rename refuses an existing name.
    else:
        os.link(source, destination)
        source.unlink()


def _isolate(path: Path, folder: Path) -> Path:
    # A newly allocated private directory owns this namespace. In particular,
    # no pre-existing recovery file is ever used as a rename destination.
    isolated = folder / "original"
    if isolated.exists() or isolated.is_symlink():
        raise FileExistsError(f"隔离位置已存在，原文件未移动：{isolated}")
    os.rename(path, isolated)
    return isolated


def _restore_isolated(isolated: Path, path: Path) -> str:
    """Restore without replacing a concurrently created user file."""
    if not isolated.exists() and not isolated.is_symlink():
        return ""
    try:
        _publish_no_replace(isolated, path)
        return ""
    except OSError:
        return f"；保留的原文件可从此处恢复：{isolated}"


def guarded_replace(path: str | Path, expected: bytes | None, writer) -> tuple[bytes, tuple | None]:
    """Stage, claim and compare the old object, then publish without replacement.

    Checking a digest followed by os.replace would still overwrite an external
    atomic replacement. Claiming the old object and exclusively publishing into
    the vacated name also protects changes made during the commit itself.
    ``writer`` keeps the caller's existing atomic writer/error contract.
    """
    path = Path(path)
    folder = Path(tempfile.mkdtemp(prefix=f".{path.name}.commit-", dir=ensure_dir(path.parent)))
    isolated, candidate = folder / "original", folder / "candidate"
    published, claimed = False, False
    try:
        writer(candidate)
        fingerprint = file_fingerprint(candidate)
        if fingerprint is None:
            raise OSError("写入未生成候选文件")
        try:
            _isolate(path, folder)
            claimed = True
        except FileNotFoundError:
            if expected is not None:
                raise DataFileError(path, "数据文件已被外部删除，已停止覆盖") from None
        if file_fingerprint(isolated) != expected:
            raise DataFileError(path, "数据文件已被外部替换，已停止覆盖")
        try:
            _publish_no_replace(candidate, path)
        except FileExistsError as exc:
            raise DataFileError(path, "提交期间出现外部新文件，已停止覆盖") from exc
        published = True
        revision = file_revision(path)
        if file_fingerprint(path) != fingerprint:
            raise DataFileError(path, "提交期间数据文件已被外部替换，已停止覆盖")
        return fingerprint, revision
    except BaseException as exc:
        recovery = _restore_isolated(isolated, path) if claimed and not published else ""
        if recovery:
            raise DataFileError(path, str(exc) + recovery) from exc
        raise
    finally:
        with suppress(OSError):
            candidate.unlink(missing_ok=True)
        if published:
            with suppress(OSError):
                isolated.unlink(missing_ok=True)
        with suppress(OSError):
            folder.rmdir()


def remove_registered_artifact(path: str | Path, expected_digest: str) -> None:
    """Delete only the claimed object; preserve an external replacement to recover."""
    path = Path(path)
    prefix = f".artifact-cleanup-{path.name}-"
    recoveries = list(path.parent.glob(prefix + "*/original"))
    if recoveries:
        raise ValueError("有未恢复的活动文件，登记已保留；请先检查：" + "，".join(map(str, recoveries)))
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=ensure_dir(path.parent)))
    isolated = folder / "original"
    claimed = False
    try:
        try:
            _isolate(path, folder)
            claimed = True
        except FileNotFoundError:
            return
        if isolated.is_symlink() or not isolated.is_file():
            raise ValueError("活动产物对象不是普通文件，已保留")
        if artifact_digest(isolated.read_bytes()) != expected_digest:
            raise ValueError("活动路径已有不同内容，已保留用户文件")
        isolated.unlink()
        if path.exists() or path.is_symlink():
            raise ValueError(f"清理期间出现同名新文件，已保留：{path}")
    except BaseException as exc:
        recovery = _restore_isolated(isolated, path) if claimed else ""
        if recovery:
            raise ValueError(str(exc) + recovery) from exc
        raise
    finally:
        with suppress(OSError):
            folder.rmdir()


def publish_artifact(path: str | Path, content: str | bytes) -> Path:
    """Publish one stable activity output, or verify an earlier identical commit.

    A crash between publishing and updating the activity journal is recovered by
    comparing bytes at this deterministic path. A different existing file is
    never replaced. Callers must choose and persist the path before this call.
    """
    destination = Path(path)
    folder = ensure_dir(destination.parent)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    if destination.is_symlink():
        raise ValueError("活动产物路径不能是符号链接")
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != payload:
            raise FileExistsError("活动产物路径已有不同内容，保留原文件")
        return destination
    fd, name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
        except FileExistsError:
            if destination.is_symlink() or destination.read_bytes() != payload:
                raise
        return destination
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def artifact_digest(content: str | bytes) -> str:
    payload = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(payload).hexdigest()


def create_artifact(directory: str | Path, stem: str, suffix: str, content: str | bytes) -> Path:
    """Flush a private temporary file, then publish to a unique, unused name.

    Windows rename refuses an existing destination. On POSIX, link publishes
    the completed inode with the same no-replace guarantee. A name collision
    never destroys an existing artifact, including across processes.
    """
    folder = ensure_dir(directory)
    payload = content.encode("utf-8") if isinstance(content, str) else content
    fd, name = tempfile.mkstemp(prefix=".artifact-", suffix=".tmp", dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for _ in range(10):
            destination = folder / f"{stem}_{uuid.uuid4().hex}{suffix}"
            try:
                if os.name == "nt":
                    os.rename(temporary, destination)
                else:
                    os.link(temporary, destination)
                return destination
            except FileExistsError:
                continue
        raise FileExistsError("无法为产物分配唯一文件名；已有文件已保留")
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)

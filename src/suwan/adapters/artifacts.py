"""Create independent artifacts atomically without replacing earlier output."""
from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import suppress
from pathlib import Path

from suwan.adapters.filestore import ensure_dir


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

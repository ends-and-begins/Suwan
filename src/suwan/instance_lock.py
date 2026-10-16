"""同一数据目录只允许一个写入实例；OS 锁在进程结束后自动释放。"""
from __future__ import annotations

import errno
import os
import threading
from pathlib import Path
from typing import BinaryIO


class InstanceAlreadyRunningError(RuntimeError):
    """另一进程或 runtime 已占用此数据目录。"""


class InstanceLock:
    """持有整个 runtime 生命周期；锁文件常驻，绝不删除后重建锁文件。"""

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir).resolve() / ".suwan.lock"
        self._file: BinaryIO | None = None
        self._guard = threading.Lock()

    def acquire(self) -> InstanceLock:
        with self._guard:
            if self._file is not None:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.close()
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise InstanceAlreadyRunningError(
                        f"数据目录 {self.path.parent} 已由另一实例使用，请先关闭该实例"
                    ) from exc
                raise
            except BaseException:
                handle.close()
                raise
            self._file = handle
        return self

    def release(self) -> None:
        with self._guard:
            handle, self._file = self._file, None
            if handle is None:
                return
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:  # noqa: ANN001
        self.release()

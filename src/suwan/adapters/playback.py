"""本地音频播放控制：后台启动、可停止，关闭后拒绝迟到的播放结果。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

_wave_guard = threading.Lock()
_wave_current = None


def _start_local_audio(path: Path):
    # MiMo 当前合成 WAV；Windows 可用标准库播放，无需加载额外播放器。
    if os.name == "nt" and path.suffix.lower() == ".wav":
        import winsound

        class WaveSound:
            def stop(self) -> None:
                global _wave_current
                with _wave_guard:
                    if _wave_current is self:
                        winsound.PlaySound(None, 0)
                        _wave_current = None

        global _wave_current
        with _wave_guard:
            sound = WaveSound()
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            _wave_current = sound
        return sound
    from playsound3 import playsound

    sound = playsound(str(path), block=False)
    if not callable(getattr(sound, "stop", None)):
        raise RuntimeError("播放器需要 playsound3>=3.0，请更新依赖")
    return sound


class AudioPlayback:
    """每次新播放替换上一条，所有可能阻塞的播放器调用都在后台。"""

    def __init__(self, starter: Callable | None = None) -> None:
        self._starter = starter or _start_local_audio
        self._guard = threading.Lock()
        self._backend_guard = threading.Lock()
        self._generation = 0
        self._closed = False
        self._sound = None
        self._pending = 0
        self._stop_error: str | None = None

    @property
    def idle(self) -> bool:
        with self._guard:
            return self._pending == 0 and self._sound is None

    @property
    def stop_error(self) -> str | None:
        with self._guard:
            return self._stop_error

    def _background(self, callback) -> None:
        with self._guard:
            self._pending += 1

        def run() -> None:
            try:
                callback()
            finally:
                with self._guard:
                    self._pending -= 1

        worker = threading.Thread(target=run, name="suwan-playback", daemon=True)
        try:
            worker.start()
        except Exception:
            with self._guard:
                self._pending -= 1
            raise

    def _stop_sound(self, sound) -> None:
        if sound is not None:
            try:
                sound.stop()
            except Exception as exc:
                with self._guard:
                    self._stop_error = f"{type(exc).__name__}: {exc}"

    def stop(self, *, close: bool = False) -> None:
        with self._guard:
            self._closed |= close
            self._generation += 1
            sound, self._sound = self._sound, None
        if sound is not None:
            self._background(lambda: self._stop_sound(sound))

    def play(self, filepath: str, *, on_started: Callable | None = None, on_error: Callable | None = None) -> None:
        path = Path(filepath)
        if not path.is_file():
            raise FileNotFoundError(f"音频文件不存在：{path.name}")
        with self._guard:
            if self._closed:
                raise RuntimeError("播放器已关闭")
            self._generation += 1
            generation = self._generation
            old, self._sound = self._sound, None

        def launch() -> None:
            self._stop_sound(old)
            try:
                # 顺序启动，防止较旧的慢播放器在新播放之后才开始发声。
                with self._backend_guard:
                    with self._guard:
                        if self._closed or generation != self._generation:
                            return
                    sound = self._starter(path)
                    with self._guard:
                        stale = self._closed or generation != self._generation
                        if not stale:
                            self._sound = sound
                    if stale:
                        self._stop_sound(sound)
                    elif on_started is not None:
                        on_started()
            except Exception as exc:
                with self._guard:
                    current = not self._closed and generation == self._generation
                if current and on_error is not None:
                    on_error(f"{type(exc).__name__}: {exc}")

        self._background(launch)

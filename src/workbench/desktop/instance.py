"""同一个数据目录只允许一个实例，避免两个进程同时把对方的任务标记为中断。"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path


class SingleInstance:
    def __init__(self, lock_file: Path):
        self.lock_file = lock_file
        self._fd = None

    def acquire(self) -> bool:
        self._fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            return False
        os.ftruncate(self._fd, 0)
        os.write(self._fd, str(os.getpid()).encode())
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

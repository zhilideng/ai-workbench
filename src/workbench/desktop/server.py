"""在后台线程里运行 uvicorn，监听 127.0.0.1 上的固定端口（被占用时退回随机端口）。

端口要尽量固定：页面的 localStorage 按「地址 + 端口」保存，端口每次都变的话，
上次选的项目、引擎、思考等级等每次启动都会丢（也就没法在打开应用时就预热）。
"""
from __future__ import annotations

import hashlib
import socket
import threading
import time
from typing import Optional

import uvicorn


def preferred_port(key: str) -> int:
    """由数据目录算出的固定端口（41000–48999），不同数据目录（正式 / 开发）互不冲突。"""
    return 41000 + int(hashlib.sha256(key.encode()).hexdigest(), 16) % 8000


class EmbeddedServer:
    def __init__(self, app, host: str = "127.0.0.1", port: Optional[int] = None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 允许绑定刚退出的上一个实例留下的 TIME_WAIT 端口；有进程正在监听时仍会绑定失败（实测 macOS）
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((host, port or 0))
        except OSError:   # 固定端口被占用（如同时开着开发版）：退回随机端口，这次启动的界面设置不会保留
            self.sock.close()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind((host, 0))
        self.host, self.port = self.sock.getsockname()[:2]
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False,
                                                    timeout_graceful_shutdown=5))
        self.thread = threading.Thread(target=self._serve, name="workbench-api", daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _serve(self) -> None:
        self.server.run(sockets=[self.sock])

    def start(self, timeout: float = 20) -> None:
        self.thread.start()
        deadline = time.time() + timeout
        while not self.server.started:
            if not self.thread.is_alive() or time.time() > deadline:
                raise RuntimeError("本地服务启动失败")
            time.sleep(0.02)

    def stop(self, timeout: float = 15) -> None:
        """触发 lifespan 关闭：停止所有正在执行的任务。"""
        self.server.should_exit = True
        self.thread.join(timeout)

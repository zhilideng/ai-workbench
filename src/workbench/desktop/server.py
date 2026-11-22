"""在后台线程里运行 uvicorn，监听 127.0.0.1 上的随机端口。"""
from __future__ import annotations

import socket
import threading
import time

import uvicorn


class EmbeddedServer:
    def __init__(self, app, host: str = "127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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

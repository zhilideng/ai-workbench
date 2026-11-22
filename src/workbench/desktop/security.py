"""本地 API 的访问控制。

API 能以你的身份执行命令，所以不能让本机其他进程或浏览器里的网页随意调用：
每次启动生成随机令牌，窗口首次加载 /?wb_token=… 时写入 SameSite=Strict 的 HttpOnly Cookie，
之后所有请求都必须带这个 Cookie。跨站请求（CSRF、DNS rebinding）带不上它。
"""
from __future__ import annotations

import hmac
import secrets
from urllib.parse import parse_qs

COOKIE = "wb_session"


def new_token() -> str:
    return secrets.token_urlsafe(32)


class TokenGuard:
    """纯 ASGI 中间件。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        qs = parse_qs(scope.get("query_string", b"").decode())
        if scope["path"] == "/" and hmac.compare_digest(qs.get("wb_token", [""])[0], self.token):
            # 首次加载：种 Cookie 并去掉 URL 里的令牌
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/"),
                                    (b"set-cookie", f"{COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict".encode())]})
            return await send({"type": "http.response.body", "body": b""})
        if hmac.compare_digest(self._cookie(scope), self.token):
            return await self.app(scope, receive, send)
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json; charset=utf-8")]})
        await send({"type": "http.response.body", "body": '{"detail":"未授权"}'.encode()})

    @staticmethod
    def _cookie(scope) -> str:
        for k, v in scope.get("headers", []):
            if k == b"cookie":
                for part in v.decode(errors="ignore").split(";"):
                    name, _, val = part.strip().partition("=")
                    if name == COOKIE:
                        return val
        return ""

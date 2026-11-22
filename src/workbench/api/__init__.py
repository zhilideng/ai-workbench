"""HTTP API（只给内置窗口用，监听 127.0.0.1）。路由只做参数解析，逻辑都在 services 里。"""
from workbench.api.app import create_app

__all__ = ["create_app"]

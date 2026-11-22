"""业务逻辑。不依赖 FastAPI：出错时抛 core.errors 里的异常，由 API 层转成 HTTP 响应。"""

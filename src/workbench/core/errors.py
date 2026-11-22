"""业务层抛出的错误。服务层不依赖 FastAPI，由 API 层统一转换成 HTTP 响应（{"detail": message}）。"""


class WorkbenchError(Exception):
    status = 500

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class BadRequest(WorkbenchError):
    status = 400


class NotFound(WorkbenchError):
    status = 404


class Conflict(WorkbenchError):
    status = 409


class PayloadTooLarge(WorkbenchError):
    status = 413


class UpstreamError(WorkbenchError):
    """外部依赖（ssh、CLI 等）失败。"""
    status = 502

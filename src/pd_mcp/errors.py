"""Structured error types shared by every MCP tool.

All tools return JSON objects:
    success: true  -> {"success": true, ...payload}
    success: false -> {"success": false, "error": {"code", "message", "details"}}
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Dict

log = logging.getLogger("pdmcp.errors")


class ErrorCode:
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
    INVALID_PARAMS = "INVALID_PARAMS"
    COM_UNAVAILABLE = "COM_UNAVAILABLE"
    COM_CALL_FAILED = "COM_CALL_FAILED"
    COM_TIMEOUT = "COM_TIMEOUT"
    OPERATION_FAILED = "OPERATION_FAILED"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    READ_ONLY = "READ_ONLY"
    TRANSACTION_ERROR = "TRANSACTION_ERROR"
    FILE_ERROR = "FILE_ERROR"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PdMcpError(Exception):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> Dict[str, Any]:
        err: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            err["details"] = self.details
        return err


class ModelNotFoundError(PdMcpError):
    def __init__(self, model_id: str):
        super().__init__(ErrorCode.MODEL_NOT_FOUND, f"Model '{model_id}' is not open", {"model_id": model_id})


class ObjectNotFoundError(PdMcpError):
    def __init__(self, kind: str, ident: str, model_id: str = ""):
        msg = f"{kind} '{ident}' does not exist" + (f" in model '{model_id}'" if model_id else "")
        super().__init__(ErrorCode.OBJECT_NOT_FOUND, msg, {"kind": kind, "ident": ident, "model_id": model_id})


class InvalidParamsError(PdMcpError):
    def __init__(self, message: str, details: Any = None):
        super().__init__(ErrorCode.INVALID_PARAMS, message, details)


class ComUnavailableError(PdMcpError):
    def __init__(self, message: str, details: Any = None):
        super().__init__(ErrorCode.COM_UNAVAILABLE, message, details)


class OperationFailedError(PdMcpError):
    def __init__(self, message: str, details: Any = None):
        super().__init__(ErrorCode.OPERATION_FAILED, message, details)


class NotSupportedError(PdMcpError):
    def __init__(self, message: str, details: Any = None):
        super().__init__(ErrorCode.NOT_SUPPORTED, message, details)


def error_response(code: str, message: str, details: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"success": False, "error": err}


def tool_result(fn: Callable) -> Callable:
    """Decorator: convert exceptions of MCP tools into structured JSON results."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PdMcpError as exc:
            return {"success": False, "error": exc.to_dict()}
        except Exception as exc:  # unexpected -> log + structured error
            log.exception("Unexpected error in tool %s", fn.__name__)
            return error_response(
                ErrorCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                {"tool": fn.__name__},
            )

    return wrapper

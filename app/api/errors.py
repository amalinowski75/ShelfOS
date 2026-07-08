"""Mapping of domain exceptions to HTTP responses.

Keeps the routers free of HTTP status concerns: services raise domain errors and
these handlers translate them into appropriate status codes.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.services.errors import (
    InsufficientStockError,
    InvoiceFinalizedError,
    NotFoundError,
    ShelfOSError,
    ValidationError,
)

_STATUS_BY_ERROR: list[tuple[type[ShelfOSError], int]] = [
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (ValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (InsufficientStockError, status.HTTP_409_CONFLICT),
    (InvoiceFinalizedError, status.HTTP_409_CONFLICT),
]


def register_error_handlers(app: FastAPI) -> None:
    """Register a JSON handler for each domain error type on the app."""

    def make_handler(status_code: int):  # type: ignore[no-untyped-def]
        async def handler(_request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})

        return handler

    for error_type, status_code in _STATUS_BY_ERROR:
        app.add_exception_handler(error_type, make_handler(status_code))

"""Domain-level exceptions shared across the service layer.

Services raise these instead of leaking database or HTTP concerns, so business
logic stays testable without HTTP or a UI (spec §3).
"""

from __future__ import annotations


class ShelfOSError(Exception):
    """Base class for all domain errors."""


class NotFoundError(ShelfOSError):
    """A referenced entity does not exist."""


class ValidationError(ShelfOSError):
    """Input violates a business rule or invariant."""


class InsufficientStockError(ShelfOSError):
    """A stock removal would drive a location's quantity below zero."""


class InvoiceFinalizedError(ShelfOSError):
    """An attempt was made to modify a finalized (read-only) invoice."""


class DuplicateComponentError(ShelfOSError):
    """A component with the same (MPN, manufacturer) already exists.

    Carries the existing component's id so the API can hand the client a link to
    it rather than only a message.
    """

    def __init__(self, message: str, *, existing_id: int) -> None:
        super().__init__(message)
        self.existing_id = existing_id

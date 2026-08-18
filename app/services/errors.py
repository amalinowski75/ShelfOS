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


class PrinterError(ShelfOSError):
    """The label printer could not be reached, or refused the job.

    Its own class, and its own 503, because nothing is wrong with the request:
    the printer is unplugged, busy, or claimed by another program. A 422 would
    tell the user to fix what they asked for, and there is nothing to fix —
    retrying in a moment is exactly the right response.

    Carries how many labels had already printed when it went wrong. A run that
    fails half way through a cabinet leaves those on the bench, and they have
    to be accounted for exactly as a stopped run's are — the failure is not a
    reason to lose the count.
    """

    def __init__(self, message: str, *, printed: int = 0, total: int = 0) -> None:
        super().__init__(message)
        self.printed = printed
        self.total = total


class TapeMismatchError(ShelfOSError):
    """The tape asked for is not the tape the printer is holding.

    Carries both, because the answer is the user's to give: print on what is
    loaded, or go and change the roll. Refusing with only a sentence would make
    the caller parse it to offer that choice.
    """

    def __init__(self, message: str, *, requested: str, loaded: str) -> None:
        super().__init__(message)
        self.requested = requested
        self.loaded = loaded


class DuplicateComponentError(ShelfOSError):
    """A component with the same (MPN, manufacturer) already exists.

    Carries the existing component's id so the API can hand the client a link to
    it rather than only a message.
    """

    def __init__(self, message: str, *, existing_id: int) -> None:
        super().__init__(message)
        self.existing_id = existing_id

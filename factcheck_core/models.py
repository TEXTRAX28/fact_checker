from __future__ import annotations

class NoEvidenceError(Exception):
    # Raised when a claim has zero search evidence to verify against (search failed or returned nothing usable). 
    pass


class ProviderProtocolError(Exception):
    """Raised when provider output does not satisfy the expected JSON protocol."""


class WholeJobDeadlineExceeded(TimeoutError):
    """Raised when the complete fact-check has exhausted its wall-clock budget."""


class ProviderBackoffCancelled(RuntimeError):
    """Raised when cancellation interrupts a provider retry delay."""


class FactCheckResult(list):
    """List-compatible pipeline result with enough metadata for service adapters."""

    def __init__(self, values=(), *, status: str = "completed", claim_count: int = 0,
                 errors: list[dict] | None = None,
                 usage: dict | None = None,
                 extraction_stats: dict | None = None):
        super().__init__(values)
        self.status = status
        self.claim_count = claim_count
        self.errors = errors or []
        self.usage = usage or {}
        self.extraction_stats = extraction_stats or {}


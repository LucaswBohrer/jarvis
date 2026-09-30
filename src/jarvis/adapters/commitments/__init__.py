"""Commitment source adapters.

Only the local source exists in Phase 2. There is intentionally no
CloneCobradorSource: see ports/commitments.py.
"""

from .local import LocalCommitmentSource

__all__ = ["LocalCommitmentSource"]

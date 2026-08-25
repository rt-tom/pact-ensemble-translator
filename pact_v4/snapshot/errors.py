"""Errors for the media-side book-state snapshot store."""


class SnapshotError(Exception):
    """Base error for book-state snapshot store operations."""


class ValidationError(SnapshotError):
    """Manifest, layout, or state-file validation failed."""


class LeaseHeld(SnapshotError):
    """A promote-time lease is already held; manual release required."""


class StaleParent(SnapshotError):
    """Candidate parent_revision_id does not match the current revision (fail-closed)."""


class HashMismatch(SnapshotError):
    """A state file's actual byte hash/size differs from the manifest."""

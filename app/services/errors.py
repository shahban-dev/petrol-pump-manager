"""Service-layer exceptions.

Services raise these (never raw ``sqlite3`` errors) so the UI can show a
clear message and tests can assert on intent. ``sqlite3.IntegrityError``
from a trigger/constraint is translated to the matching subclass — the
trigger remains the backstop, the service just explains it first.

Bilingual contract (Phase 6): every exception may carry a stable
``message_key`` (e.g. ``"err.invalid_business_date"``) plus ``params`` —
raw domain values (integer paisa, integer milliliters, ``YYYY-MM-DD``
dates, names/codes). The UI renders ``tr(message_key).format(**params)``
with display-time formatting; ``str(exc)`` always returns the English
message so existing tests and non-UI consumers keep working.
"""

from __future__ import annotations


class BusinessRuleError(Exception):
    """A business rule refused the operation. Nothing was committed."""

    def __init__(
        self,
        message: str = "",
        *,
        key: str | None = None,
        params: dict | None = None,
    ) -> None:
        super().__init__(message)
        # Stable localization key, e.g. "err.invalid_business_date".
        self.message_key = key
        # Raw values for the key's named placeholders; formatted at
        # display time by ``app.localization.localize_error``.
        self.params = dict(params) if params else {}


class ValidationError(BusinessRuleError):
    """Input or state validation failed before any write was attempted."""


class NotFoundError(BusinessRuleError):
    """A referenced record (day, nozzle, reading, …) does not exist."""


class DuplicateError(BusinessRuleError):
    """The operation would create a duplicate guarded by a unique rule.

    Raised instead of re-inserting when a *different* value already exists;
    an identical retry is idempotent and returns the existing record.
    """


class ClosedDayError(BusinessRuleError):
    """The target business day is closed (or non-operating)."""


class CloseBlockedError(ValidationError):
    """The guided-closing checklist has blocking errors.

    Carries the full :class:`CloseValidationReport` so the UI can render
    the checklist instead of a bare message.
    """

    def __init__(
        self,
        message: str,
        report,
        *,
        key: str | None = None,
        params: dict | None = None,
    ) -> None:
        super().__init__(message, key=key, params=params)
        self.report = report


class BackupError(BusinessRuleError):
    """A backup or restore operation failed.

    Every step of the backup pipeline records its outcome in
    ``backup_history`` where possible, so a ``BackupError`` always comes
    with a history row explaining how far the pipeline got.
    """


class RestoreConfirmationRequired(BackupError):
    """A restore was requested without explicit confirmation.

    Restoring replaces the live database, so the UI must collect a
    deliberate, explicit confirmation from the manager first.
    """


class BackupValidationError(BackupError):
    """A backup file failed integrity/consistency validation.

    The file is not used as a restore candidate and is never promoted to
    the live database.
    """

"""Database layer: pragma-verified connections, versioned migrations, triggers."""

from .connection import DatabaseError, DatabaseManager
from .integrity_checks import (
    check_foreign_keys,
    check_integrity,
    check_required_objects,
)
from .migrator import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS_DIR,
    MigrationError,
    get_schema_version,
    migrate,
    verify_schema,
)

__all__ = [
    "DatabaseError",
    "DatabaseManager",
    "MigrationError",
    "MIGRATIONS_DIR",
    "CURRENT_SCHEMA_VERSION",
    "get_schema_version",
    "migrate",
    "verify_schema",
    "check_integrity",
    "check_foreign_keys",
    "check_required_objects",
]

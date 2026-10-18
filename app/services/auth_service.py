"""Owner authentication (v2).

Single-owner offline model: the first launch creates the owner account
(username + password + two security questions). Every later launch
requires the password. Password reset is question-based — there is no
email or SMS in this offline app.

Password storage: ``hashlib.scrypt`` (stdlib, no new dependency),
per-user 16-byte salt, hex-encoded. Verification uses
``hmac.compare_digest`` (constant time). Plaintext passwords never touch
the database.

Lockout: 5 consecutive bad attempts lock the account for 15 minutes.
The users table deliberately has NO audit_log triggers — audit snapshots
would capture password/security-answer hashes (migration 016).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.services import errors
from app.services._base import ServiceBase

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 15
_MIN_PASSWORD_LEN = 8


class AuthError(errors.BusinessRuleError):
    """Wrong credentials, locked account, or unknown user."""


@dataclass(frozen=True)
class UserInfo:
    uuid: str
    username: str
    role: str
    last_login_at_utc: str | None


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _hash_secret(secret: str, salt_hex: str) -> str:
    """scrypt hex digest of *secret* with the given hex salt."""
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return digest.hex()


def _new_hash(secret: str) -> tuple[str, str]:
    """Return ``(salt_hex, hash_hex)`` for a new secret."""
    salt_hex = secrets.token_hex(_SALT_BYTES)
    return salt_hex, _hash_secret(secret, salt_hex)


def _normalize_answer(answer: str) -> str:
    """Security answers compare case-insensitively, whitespace-trimmed."""
    return answer.strip().casefold()


class AuthService(ServiceBase):
    """Owner account management and authentication."""

    # ------------------------------------------------------------------
    # setup / state
    # ------------------------------------------------------------------
    def needs_setup(self) -> bool:
        """True when no user account exists yet (first launch)."""
        with self.read_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) == 0

    def setup_owner(
        self,
        username: str,
        password: str,
        security_q1: str,
        security_a1: str,
        security_q2: str,
        security_a2: str,
        *,
        actor: str | None = None,
    ) -> UserInfo:
        """Create the owner account. Only allowed when no users exist.

        Raises:
            BusinessRuleError: if an account already exists.
            ValidationError: on blank fields, weak password, or duplicate
                security questions.
        """
        clean_username = (username or "").strip()
        if not clean_username:
            raise errors.ValidationError(
                "Username must not be empty.", key="err.auth_username_empty"
            )
        self._require_password_strength(password)
        q1 = (security_q1 or "").strip()
        q2 = (security_q2 or "").strip()
        if not q1 or not q2:
            raise errors.ValidationError(
                "Both security questions are required.",
                key="err.auth_questions_required",
            )
        if q1 == q2:
            raise errors.ValidationError(
                "The two security questions must be different.",
                key="err.auth_questions_different",
            )
        if not _normalize_answer(security_a1 or "") or not _normalize_answer(
            security_a2 or ""
        ):
            raise errors.ValidationError(
                "Both security answers are required.",
                key="err.auth_answers_required",
            )
        who = self.resolve_actor(actor)
        pw_salt, pw_hash = _new_hash(password)
        a1_salt, a1_hash = _new_hash(_normalize_answer(security_a1))
        a2_salt, a2_hash = _new_hash(_normalize_answer(security_a2))
        user_uuid = _uuid.uuid4().hex
        with self.transaction() as conn:
            existing = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            if int(existing["n"]) > 0:
                raise errors.BusinessRuleError(
                    "An owner account already exists.",
                    key="err.auth_already_setup",
                )
            try:
                conn.execute(
                    "INSERT INTO users"
                    " (uuid, username, password_hash, password_salt, role,"
                    "  security_q1, security_a1_hash, security_a1_salt,"
                    "  security_q2, security_a2_hash, security_a2_salt,"
                    "  created_by)"
                    " VALUES (?, ?, ?, ?, 'owner', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_uuid,
                        clean_username,
                        pw_hash,
                        pw_salt,
                        q1,
                        a1_hash,
                        a1_salt,
                        q2,
                        a2_hash,
                        a2_salt,
                        who,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise errors.DuplicateError(
                    f"Username {clean_username!r} is already taken.",
                    key="err.auth_username_taken",
                ) from exc
        return UserInfo(
            uuid=user_uuid, username=clean_username, role="owner",
            last_login_at_utc=None,
        )

    # ------------------------------------------------------------------
    # authentication
    # ------------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> UserInfo:
        """Verify credentials. Returns the user on success.

        On a wrong password the failed-attempt counter increments; after
        5 consecutive failures the account locks for 15 minutes.
        Raises:
            AuthError: unknown user, locked account, or wrong password.
        """
        clean_username = (username or "").strip()
        # NOTE: the failed-password path writes (failed_attempts/lockout)
        # and THEN raises — the raise happens after the transaction block
        # commits, otherwise the counter update would be rolled back.
        auth_failed = False
        user_info: UserInfo | None = None
        with self.transaction() as conn:
            row = self._get_user_row(conn, clean_username)
            if row is None:
                # Same error as a wrong password: never reveal which half
                # of the credential pair was wrong.
                raise AuthError(
                    "Invalid username or password.",
                    key="err.auth_invalid_credentials",
                )
            locked_until = row["locked_until_utc"]
            if locked_until and locked_until > _utc_now_iso():
                raise AuthError(
                    "Account is temporarily locked after too many failed"
                    " attempts. Try again later.",
                    key="err.auth_locked",
                )
            if not hmac.compare_digest(
                _hash_secret(password or "", row["password_salt"]),
                row["password_hash"],
            ):
                failed = int(row["failed_attempts"]) + 1
                if failed >= _MAX_FAILED_ATTEMPTS:
                    locked_until = (
                        datetime.now(timezone.utc)
                        + timedelta(minutes=_LOCKOUT_MINUTES)
                    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                    conn.execute(
                        "UPDATE users SET failed_attempts = ?,"
                        " locked_until_utc = ? WHERE id = ?",
                        (failed, locked_until, int(row["id"])),
                    )
                else:
                    conn.execute(
                        "UPDATE users SET failed_attempts = ? WHERE id = ?",
                        (failed, int(row["id"])),
                    )
                auth_failed = True
            else:
                now = _utc_now_iso()
                conn.execute(
                    "UPDATE users SET failed_attempts = 0,"
                    " locked_until_utc = NULL, last_login_at_utc = ?"
                    " WHERE id = ?",
                    (now, int(row["id"])),
                )
                user_info = UserInfo(
                    uuid=row["uuid"],
                    username=row["username"],
                    role=row["role"],
                    last_login_at_utc=now,
                )
        if auth_failed:
            raise AuthError(
                "Invalid username or password.",
                key="err.auth_invalid_credentials",
            )
        assert user_info is not None
        return user_info

    def change_password(
        self,
        username: str,
        old_password: str,
        new_password: str,
        *,
        actor: str | None = None,
    ) -> None:
        """Change password when logged in (old password required)."""
        self._require_password_strength(new_password)
        clean_username = (username or "").strip()
        with self.transaction() as conn:
            row = self._get_user_row(conn, clean_username)
            if row is None or not hmac.compare_digest(
                _hash_secret(old_password or "", row["password_salt"]),
                row["password_hash"],
            ):
                raise AuthError(
                    "The current password is incorrect.",
                    key="err.auth_current_password_wrong",
                )
            salt_hex, hash_hex = _new_hash(new_password)
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?,"
                " failed_attempts = 0, locked_until_utc = NULL"
                " WHERE id = ?",
                (hash_hex, salt_hex, int(row["id"])),
            )

    # ------------------------------------------------------------------
    # password reset (security questions)
    # ------------------------------------------------------------------
    def security_questions(self, username: str) -> tuple[str, str]:
        """The two security questions for *username* (for the reset UI)."""
        with self.read_connection() as conn:
            row = self._get_user_row(conn, (username or "").strip())
        if row is None:
            raise AuthError(
                "Invalid username or password.",
                key="err.auth_invalid_credentials",
            )
        return row["security_q1"], row["security_q2"]

    def verify_answers(self, username: str, answer1: str, answer2: str) -> None:
        """Check both security answers without resetting anything.

        Raises:
            AuthError: if the answers do not match.
        """
        clean_username = (username or "").strip()
        with self.read_connection() as conn:
            row = self._get_user_row(conn, clean_username)
        if row is None:
            raise AuthError(
                "Invalid username or password.",
                key="err.auth_invalid_credentials",
            )
        ok1 = hmac.compare_digest(
            _hash_secret(
                _normalize_answer(answer1 or ""), row["security_a1_salt"]
            ),
            row["security_a1_hash"],
        )
        ok2 = hmac.compare_digest(
            _hash_secret(
                _normalize_answer(answer2 or ""), row["security_a2_salt"]
            ),
            row["security_a2_hash"],
        )
        if not (ok1 and ok2):
            raise AuthError(
                "The security answers do not match.",
                key="err.auth_answers_wrong",
            )

    def reset_password(
        self,
        username: str,
        answer1: str,
        answer2: str,
        new_password: str,
        *,
        actor: str | None = None,
    ) -> None:
        """Reset the password after answering both security questions.

        A successful reset also clears any lockout.
        """
        self._require_password_strength(new_password)
        # Verify answers first (raises AuthError on mismatch).
        self.verify_answers(username, answer1, answer2)
        clean_username = (username or "").strip()
        with self.transaction() as conn:
            row = self._get_user_row(conn, clean_username)
            if row is None:
                raise AuthError(
                    "Invalid username or password.",
                    key="err.auth_invalid_credentials",
                )
            salt_hex, hash_hex = _new_hash(new_password)
            conn.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?,"
                " failed_attempts = 0, locked_until_utc = NULL"
                " WHERE id = ?",
                (hash_hex, salt_hex, int(row["id"])),
            )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _get_user_row(
        conn: sqlite3.Connection, username: str
    ) -> sqlite3.Row | None:
        # DatabaseManager already sets row_factory = sqlite3.Row.
        return conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()

    @staticmethod
    def _require_password_strength(password: str) -> None:
        if not password or len(password) < _MIN_PASSWORD_LEN:
            raise errors.ValidationError(
                f"Password must be at least {_MIN_PASSWORD_LEN} characters.",
                key="err.auth_password_too_short",
            )
